"""Search workers: take a job off the queue, sweep its want, record the outcome.

Delivery is at-least-once and workers die, so every path through ``handle``
is safe to repeat. The job record in the store, not the message, decides
whether work happens: a finished job is never redone, a job leased by a live
worker is deferred, and a lease that ran out means its worker died and the job
is taken over.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Literal

from ..client import WizzClient
from ..network import RouteNetwork
from ..store import (
    JOB_DEAD,
    JOB_DONE,
    JOB_FAILED,
    JOB_FINISHED_STATES,
    RUN_ERROR,
    RUN_INCOMPLETE,
    RUN_OK,
    Store,
)
from ..sweep import PermanentJobError, sweep_want
from .queue import JobQueue

log = logging.getLogger(__name__)
UTC = timezone.utc

HEARTBEAT_EVERY_S = 30.0


@dataclass
class Outcome:
    action: Literal["ack", "retry"]
    delay_s: int = 0
    reason: str = ""


def backoff_s(attempt: int) -> int:
    """Seconds before a failed job is tried again: 30, 60, 120 ... capped at 15 min."""
    return min(30 * 2 ** (max(attempt, 1) - 1), 900)


def _job_key(body: dict[str, Any] | str) -> str | None:
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            return None
    if not isinstance(body, dict):
        return None
    key = body.get("job")
    return key if isinstance(key, str) and key else None


class JobRunner:
    def __init__(
        self,
        store: Store,
        client: WizzClient,
        network: Callable[[], RouteNetwork],
        worker_id: str,
        lease_s: float,
        max_receives: int = 5,
    ) -> None:
        self.store = store
        self.client = client
        self.network = network
        self.worker_id = worker_id
        self.lease_s = lease_s
        self.max_receives = max_receives

    def handle(self, body: dict[str, Any] | str, receive_count: int) -> Outcome:
        key = _job_key(body)
        if key is None:
            log.warning("dropping malformed message: %r", body)
            return Outcome("ack", reason="malformed")

        job = self.store.get_job(key)
        if job is None:
            log.warning("dropping message for unknown job %s", key)
            return Outcome("ack", reason="unknown job")
        if job.status in JOB_FINISHED_STATES:
            return Outcome("ack", reason="duplicate")

        want = self.store.get_want(job.want_id)
        if want is None or not want.active:
            why = "want was deleted" if want is None else "want is paused"
            self.store.finish_job(key, JOB_FAILED, error=why)
            return Outcome("ack", reason=why)

        claimed = self.store.claim_job(key, self.worker_id, self.lease_s)
        if claimed is None:
            return self._deferred(key)

        run_id = self.store.start_search_run(want.id, key)
        requests, hits = self.client.stats.requests, self.client.stats.cache_hits
        try:
            result = sweep_want(self.store, self.client, self.network(), want)
        except (PermanentJobError, sqlite3.IntegrityError) as exc:
            self.store.finish_search_run(run_id, want.id, RUN_ERROR)
            self.store.finish_job(key, JOB_FAILED, error=str(exc))
            return Outcome("ack", reason="permanent failure")
        except Exception as exc:  # noqa: BLE001 - every other failure is worth a retry
            log.warning("job %s attempt %d failed: %s", key, claimed.attempts, exc)
            self.store.finish_search_run(run_id, want.id, RUN_ERROR)
            if receive_count >= self.max_receives:
                self.store.finish_job(key, JOB_DEAD, error=str(exc))
                self.store.log(
                    "search",
                    f"Gave up on {key} after {receive_count} attempts: {exc}",
                    level="error",
                    want_id=want.id,
                )
            else:
                self.store.release_job(key, str(exc))
            return Outcome("retry", backoff_s(claimed.attempts), "transient failure")

        self.store.finish_search_run(
            run_id,
            want.id,
            RUN_OK if result.complete else RUN_INCOMPLETE,
            paths_considered=result.paths_considered,
            routes_queried=result.routes_queried,
            upstream_calls=self.client.stats.requests - requests,
            cache_hits=self.client.stats.cache_hits - hits,
            itineraries=result.itineraries,
            new_candidates=result.new_candidates,
            failed_routes=result.failed_routes,
        )
        self.store.finish_job(key, JOB_DONE, result=result.to_dict())
        return Outcome("ack", reason="done")

    def _deferred(self, key: str) -> Outcome:
        """Someone else holds the job. Look again when their lease runs out."""
        current = self.store.get_job(key)
        if current is None or current.status in JOB_FINISHED_STATES:
            return Outcome("ack", reason="duplicate")
        remaining = 1
        if current.lease_until is not None:
            left = (current.lease_until - datetime.now(UTC)).total_seconds()
            remaining = max(1, math.ceil(left))
        return Outcome("retry", remaining, "leased by another worker")


async def run_worker(
    queue: JobQueue,
    runner: JobRunner,
    stop: asyncio.Event,
    idle_s: float = 1.0,
) -> None:
    """Pull and handle jobs until ``stop`` is set."""
    last_beat = 0.0
    while not stop.is_set():
        if time.monotonic() - last_beat >= HEARTBEAT_EVERY_S:
            await asyncio.to_thread(runner.store.beat, f"worker:{runner.worker_id}")
            last_beat = time.monotonic()

        messages = await asyncio.to_thread(queue.receive, 1, idle_s)
        for message in messages:
            try:
                outcome = await asyncio.to_thread(
                    runner.handle, message.body, message.receive_count
                )
            except Exception:  # noqa: BLE001
                # Leave the message alone: its lease runs out and it comes back.
                log.exception("worker %s could not handle %s", runner.worker_id, message.id)
                continue
            if outcome.action == "ack":
                await asyncio.to_thread(queue.ack, message)
            else:
                await asyncio.to_thread(queue.retry_later, message, outcome.delay_s)
