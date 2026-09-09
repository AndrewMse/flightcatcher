"""The long-running service: the booker, web UI, Discord bot and search workers.

The booker, web UI and Discord bot share one process because they share
mutable state that is awkward to split: the watcher holds a live browser parked
at a confirm button, and both the web UI and Discord need to resolve an
approval against *that* in-memory hold. Passing approvals between processes
would mean the hold could outlive the thing that owns it, which is exactly the
failure this design is trying to avoid.

Searches are different. They are stateless, so they go through the job queue
and run on search workers: by default one inside this process, any number more
via ``hopwatch worker``, or on Lambda when the backend is AWS.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket
from typing import Any

from .backend import Backend, make_client, open_backend
from .jobs.worker import JobRunner, network_loader, run_worker
from .notify import CompositeNotifier, Notifier, StoreNotifier
from .settings import Settings
from .watcher import Watcher

log = logging.getLogger(__name__)


class Service:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.backend = open_backend(settings)
        self.store = self.backend.store
        self.watcher: Watcher | None = None
        self._workers_stop = asyncio.Event()
        self.discord: Any | None = None
        self._web_server: Any | None = None
        self._stopping = asyncio.Event()

    # --- assembly -----------------------------------------------------------

    def _build_notifier(self) -> Notifier:
        notifiers: list[Notifier] = [StoreNotifier(self.store)]

        if self.settings.discord.enabled:
            try:
                from .discord_bot import DiscordNotifier

                self.discord = DiscordNotifier(
                    self.settings.discord,
                    self.store,
                    on_decision=self._on_decision,
                    status_provider=lambda: self.watcher.status() if self.watcher else {},
                )
                notifiers.append(self.discord)
            except Exception as exc:  # noqa: BLE001
                log.error("Discord disabled: %s", exc)
                self.store.log("discord", f"Discord disabled: {exc}", level="error")

        return CompositeNotifier(notifiers)

    def _on_decision(self, booking_id: int, approved: bool, by: str) -> bool:
        if self.watcher is None:
            return False
        return self.watcher.decide(booking_id, approved, by)

    # --- run ----------------------------------------------------------------

    async def run(self) -> None:
        for problem in self.settings.describe_problems():
            log.warning("config: %s", problem)
            self.store.log("config", problem, level="warning")

        notifier = self._build_notifier()
        self.watcher = Watcher(self.store, self.settings, notifier, queue=self.backend.queue)

        tasks: list[asyncio.Task] = []

        if self.discord is not None:
            await self.discord.start()

        tasks.append(asyncio.create_task(self.watcher.start(), name="watcher"))
        tasks.extend(
            start_workers(
                self.settings, self.backend, self.settings.workers.in_process, self._workers_stop
            )
        )

        if self.settings.web.enabled:
            tasks.append(asyncio.create_task(self._serve_web(), name="web"))

        self._install_signal_handlers()

        log.info("Hopwatch running")
        if self.settings.web.enabled:
            log.info(
                "Web UI at http://%s:%d",
                self.settings.web.host,
                self.settings.web.port,
            )

        stopper = asyncio.create_task(self._stopping.wait(), name="stop")
        done, pending = await asyncio.wait(
            [*tasks, stopper], return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            if task is not stopper and task.exception():
                log.error("%s died: %s", task.get_name(), task.exception())

        await self.shutdown()

        # Give uvicorn a moment to unwind its own lifespan before cancelling.
        # Cancelling it mid-shutdown makes Ctrl-C spew a CancelledError
        # traceback that looks like a crash but is just us being impatient.
        web_tasks = [t for t in pending if t.get_name() == "web"]
        if web_tasks:
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait(web_tasks, timeout=5)

        worker_tasks = [t for t in pending if t.get_name().startswith("worker-")]
        if worker_tasks:
            # Let a worker finish the job in hand rather than abandon its lease.
            await asyncio.wait(worker_tasks, timeout=30)

        for task in pending:
            if task.done():
                continue
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.backend.close()

    async def _serve_web(self) -> None:
        import uvicorn

        from .web import create_app

        app = create_app(self.store, self.settings, self.watcher, queue=self.backend.queue)
        config = uvicorn.Config(
            app,
            host=self.settings.web.host,
            port=self.settings.web.port,
            log_level="warning",
            access_log=False,
        )
        self._web_server = uvicorn.Server(config)
        await self._web_server.serve()

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self._stopping.set)

    async def shutdown(self) -> None:
        log.info("shutting down")
        self._workers_stop.set()
        if self._web_server is not None:
            self._web_server.should_exit = True
        if self.watcher is not None:
            await self.watcher.stop()
        if self.discord is not None:
            with contextlib.suppress(Exception):
                await self.discord.close()


def start_workers(
    settings: Settings, backend: Backend, count: int, stop: asyncio.Event
) -> list[asyncio.Task]:
    """Start ``count`` search workers in this process.

    Each gets its own Wizz client, so its search-run figures are its own, but
    all of them share the rate limit and cache from the backend.
    """
    tasks: list[asyncio.Task] = []
    if count <= 0:
        return tasks
    load_network = network_loader(make_client(settings, backend))
    host = socket.gethostname().split(".")[0]
    for i in range(count):
        runner = JobRunner(
            backend.store,
            make_client(settings, backend),
            network=load_network,
            worker_id=f"{host}-{os.getpid()}-{i}",
            lease_s=settings.queue.visibility_s,
            max_receives=settings.queue.max_receives,
        )
        tasks.append(
            asyncio.create_task(run_worker(backend.queue, runner, stop), name=f"worker-{i}")
        )
    log.info("started %d search worker(s)", len(tasks))
    return tasks


async def run_service(settings: Settings) -> None:
    await Service(settings).run()


async def run_workers(settings: Settings, concurrency: int) -> None:
    """``hopwatch worker``: search workers only, until SIGINT or SIGTERM."""
    backend = open_backend(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    tasks = start_workers(settings, backend, concurrency, stop)
    try:
        await asyncio.gather(*tasks)
    finally:
        backend.close()
