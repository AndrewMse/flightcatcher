"""One benchmark worker process.

``--mode shared`` is the production path: exactly what ``hopwatch worker``
runs, with the rate limit and cache shared through the backend.

``--mode naive`` is the design this project avoids, kept here only to measure
it: each process gets its own in-memory rate limit and private cache, the way
"just start more workers" usually turns out.

Settings come from HOPWATCH_* environment variables, set by the harness.
With ``--counts FILE``, every AWS API call is counted and written to FILE on
exit, for the cost model.
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import json
import math
import os
import signal
import socket
from collections import Counter
from pathlib import Path
from typing import Any

from hopwatch import config
from hopwatch.backend import open_backend
from hopwatch.cache import DiskCache
from hopwatch.client import WizzClient
from hopwatch.jobs.worker import JobRunner, network_loader, run_worker
from hopwatch.logs import configure
from hopwatch.ratelimit import MemoryRateLimiter
from hopwatch.service import start_workers
from hopwatch.settings import Settings

DYNAMO_WRITES = {"PutItem", "UpdateItem", "DeleteItem"}
DYNAMO_READS = {"GetItem", "Query", "Scan"}


def _size(value: Any) -> int:
    """Approximate stored size in bytes; binary values count as their length."""
    return len(json.dumps(value, default=lambda o: "x" * len(o) if isinstance(o, (bytes, bytearray)) else str(o)))


class AwsCallCounter:
    """Counts AWS calls and estimates DynamoDB capacity units from payload sizes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.calls: Counter[str] = Counter()
        self.read_units = 0.0
        self.write_units = 0.0

    def install(self) -> None:
        import boto3

        boto3.setup_default_session()
        events = boto3.DEFAULT_SESSION.events
        events.register("before-call.*.*", self._before)
        events.register("after-call.*.*", self._after)
        atexit.register(self.dump)

    def _before(self, model: Any, params: dict[str, Any], **_: Any) -> None:
        service = model.service_model.service_name
        self.calls[f"{service}:{model.name}"] += 1
        if service != "dynamodb":
            return
        if model.name in DYNAMO_WRITES:
            self.write_units += max(1, math.ceil(_size(params) / 1024))
        elif model.name == "TransactWriteItems":
            for item in params.get("TransactItems", []):
                self.write_units += 2 * max(1, math.ceil(_size(item) / 1024))

    def _after(self, model: Any, parsed: dict[str, Any], **_: Any) -> None:
        if model.service_model.service_name != "dynamodb" or model.name not in DYNAMO_READS:
            return
        size = _size(parsed.get("Item") or parsed.get("Items") or {})
        self.read_units += max(0.5, math.ceil(size / 4096) * 0.5)

    def dump(self) -> None:
        self.path.write_text(json.dumps({
            "calls": dict(self.calls),
            "dynamo_read_units": self.read_units,
            "dynamo_write_units": self.write_units,
        }))


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["shared", "naive"], default="shared")
    parser.add_argument("--interval", type=float, default=config.MIN_REQUEST_INTERVAL)
    parser.add_argument("--private-cache", default=None)
    parser.add_argument("--counts", default=None)
    args = parser.parse_args()

    configure("json")
    config.MIN_REQUEST_INTERVAL = args.interval
    if args.counts:
        AwsCallCounter(Path(args.counts)).install()

    settings = Settings.from_env()
    backend = open_backend(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    if args.mode == "shared":
        tasks = start_workers(settings, backend, 1, stop)
    else:
        client = WizzClient(
            cache=DiskCache(Path(args.private_cache)),
            limiter=MemoryRateLimiter(args.interval),
            backend_url=settings.wizz.backend_url,
            homepage_url=settings.wizz.homepage_url,
        )
        runner = JobRunner(
            backend.store, client, network_loader(client),
            worker_id=f"{socket.gethostname().split('.')[0]}-{os.getpid()}-0",
            lease_s=settings.queue.visibility_s,
            max_receives=settings.queue.max_receives,
        )
        tasks = [asyncio.create_task(run_worker(backend.queue, runner, stop))]

    try:
        await asyncio.gather(*tasks)
    finally:
        backend.close()


if __name__ == "__main__":
    asyncio.run(main())
