"""Plumbing for the benchmarks: the fake backend, worker processes, a clock.

Each scenario gets a fresh directory, a fresh database or set of emulated AWS
resources, and its own worker processes, so one scenario's warm cache never
flatters another's numbers.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from hopwatch.jobs.queue import JobQueue
from hopwatch.network import RouteNetwork
from hopwatch.store import JOB_FINISHED_STATES, Store

from .fakewizz import VERSION, build_app, serve_in_thread

UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[1]
HOST = socket.gethostname().split(".")[0]
REGION = "eu-central-1"
PREFIX = "hwbench"


@dataclass
class Fake:
    url: str
    stop: Callable[[], None]

    def stats(self, timestamps: bool = False) -> dict[str, Any]:
        return httpx.get(f"{self.url}/_stats", params={"timestamps": timestamps}).json()

    def reset(self) -> None:
        httpx.post(f"{self.url}/_reset")

    def network(self) -> RouteNetwork:
        return RouteNetwork(httpx.get(f"{self.url}/{VERSION}/Api/asset/map").json())


def start_fake(**kwargs: Any) -> Fake:
    url, stop = serve_in_thread(build_app(**kwargs))
    return Fake(url, stop)


def pick_pairs(net: RouteNetwork, count: int) -> list[tuple[str, str]]:
    """Origin/destination pairs that have at least one 1-stop path, spread out."""
    codes = sorted(net.stations)
    pairs: list[tuple[str, str]] = []
    step = 7
    i = 0
    while len(pairs) < count and i < len(codes) ** 2:
        origin = codes[i % len(codes)]
        dest = codes[(i * step + 3) % len(codes)]
        i += 1
        if origin == dest or (origin, dest) in pairs:
            continue
        if net.find_paths([origin], [dest], max_stops=1, max_detour=4.0):
            pairs.append((origin, dest))
    return pairs


def seed_wants(store: Store, pairs: list[tuple[str, str]], start: date, days: int) -> list[int]:
    return [
        store.add_want(
            name=f"{origin}-{dest}", origin=origin, destination=dest,
            date_from=start.isoformat(), date_to=(start + timedelta(days=days)).isoformat(),
            max_stops=1, min_layover_min=90, max_layover_min=1200, max_detour=4.0,
            max_trip_hours=30.0, allow_ground_transfer=0, after_hour=None, before_hour=None,
            auto_request_booking=0, active=1, notes="",
        )
        for origin, dest in pairs
    ]


def enqueue(store: Store, queue: JobQueue, want_ids: list[int], tag: str) -> list[str]:
    """Queue one job per want, the way the scheduler does."""
    keys = []
    for want_id in want_ids:
        key = f"bench:{tag}:{want_id}"
        if store.create_job(key, "search_want", want_id):
            queue.send({"job": key})
        keys.append(key)
    return keys


def finished(store: Store, keys: list[str]) -> bool:
    return all((job := store.get_job(k)) is not None and job.status in JOB_FINISHED_STATES
               for k in keys)


def wait_for(predicate: Callable[[], bool], timeout: float, poll: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(poll)
    return predicate()


def local_env(workdir: Path, fake: Fake, *, visibility_s: int = 30) -> dict[str, str]:
    return {
        **os.environ,
        "HOME": str(workdir / "home"),
        "HOPWATCH_BACKEND": "local",
        "HOPWATCH_DB": str(workdir / "bench.db"),
        "HOPWATCH_CACHE": str(workdir / "cache"),
        "HOPWATCH_WIZZ_BACKEND": fake.url,
        "HOPWATCH_WIZZ_HOMEPAGE": f"{fake.url}/en-gb",
        "HOPWATCH_QUEUE_VISIBILITY_S": str(visibility_s),
        "PYTHONPATH": str(ROOT),
    }


def aws_env(workdir: Path, fake: Fake, aws: dict[str, str], *, visibility_s: int = 30) -> dict[str, str]:
    return {
        **local_env(workdir, fake, visibility_s=visibility_s),
        "HOPWATCH_BACKEND": "aws",
        "HOPWATCH_AWS_REGION": REGION,
        "HOPWATCH_TABLE_PREFIX": PREFIX,
        "HOPWATCH_AWS_ENDPOINT": aws["endpoint"],
        "HOPWATCH_QUEUE_URL": aws["queue_url"],
        "HOPWATCH_DLQ_URL": aws["dlq_url"],
        "AWS_ACCESS_KEY_ID": "bench",
        "AWS_SECRET_ACCESS_KEY": "bench",
        "AWS_DEFAULT_REGION": REGION,
    }


class Workers:
    """A pool of worker processes that can be grown, killed and respawned."""

    def __init__(self, env: dict[str, str], workdir: Path, *, mode: str = "shared",
                 interval: float = 0.2, counts: bool = False) -> None:
        self.env = env
        self.workdir = workdir
        self.mode = mode
        self.interval = interval
        self.counts = counts
        self.procs: list[subprocess.Popen] = []
        self.spawned = 0
        (workdir / "logs").mkdir(parents=True, exist_ok=True)

    def spawn(self, n: int = 1) -> None:
        for _ in range(n):
            index = self.spawned
            self.spawned += 1
            args = [sys.executable, "-m", "bench.worker_proc", "--mode", self.mode,
                    "--interval", str(self.interval)]
            if self.mode == "naive":
                args += ["--private-cache", str(self.workdir / f"private-cache-{index}")]
            if self.counts:
                args += ["--counts", str(self.workdir / f"counts-{index}.json")]
            log = open(self.workdir / "logs" / f"worker-{index}.log", "wb")
            self.procs.append(subprocess.Popen(args, env=self.env, cwd=ROOT, stderr=log,
                                               stdout=subprocess.DEVNULL))

    def worker_prefix(self, proc: subprocess.Popen) -> str:
        return f"{HOST}-{proc.pid}-"

    def kill(self, proc: subprocess.Popen) -> None:
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)
        self.procs.remove(proc)

    def stop(self) -> None:
        for proc in self.procs:
            proc.send_signal(signal.SIGTERM)
        for proc in self.procs:
            try:
                proc.wait(timeout=40)
            except subprocess.TimeoutExpired:
                proc.kill()
        self.procs.clear()

    def log_bytes(self) -> int:
        return sum(p.stat().st_size for p in (self.workdir / "logs").glob("*.log"))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_moto(visibility_s: int = 30) -> tuple[dict[str, str], Callable[[], None]]:
    """An emulated DynamoDB and SQS on localhost, with Hopwatch's tables and queues."""
    import json

    import boto3
    from moto.server import ThreadedMotoServer

    from hopwatch.aws.schema import create_tables

    import logging

    logging.getLogger("werkzeug").setLevel(logging.ERROR)  # moto's request log
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "bench")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "bench")
    port = free_port()
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=port, verbose=False)
    server.start()
    endpoint = f"http://127.0.0.1:{port}"
    create_tables(PREFIX, REGION, endpoint)
    sqs = boto3.client("sqs", region_name=REGION, endpoint_url=endpoint)
    dlq_url = sqs.create_queue(QueueName=f"{PREFIX}-searches-dlq")["QueueUrl"]
    dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["QueueArn"])[
        "Attributes"]["QueueArn"]
    queue_url = sqs.create_queue(QueueName=f"{PREFIX}-searches", Attributes={
        "VisibilityTimeout": str(visibility_s),
        "RedrivePolicy": json.dumps({"deadLetterTargetArn": dlq_arn, "maxReceiveCount": "5"}),
    })["QueueUrl"]
    return {"endpoint": endpoint, "queue_url": queue_url, "dlq_url": dlq_url}, server.stop


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * pct / 100
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def now() -> datetime:
    return datetime.now(UTC)
