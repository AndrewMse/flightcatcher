"""One politeness budget for every worker, however many there are."""

from __future__ import annotations

import threading
import time

import pytest

from hopwatch.ratelimit import MemoryRateLimiter, SqliteRateLimiter

INTERVAL = 0.05


@pytest.fixture(params=["memory", "sqlite"])
def make_limiter(request, tmp_path):
    def build(interval: float = INTERVAL):
        if request.param == "memory":
            return MemoryRateLimiter(interval)
        if request.param == "sqlite":
            return SqliteRateLimiter(tmp_path / "limit.db", interval)
        raise AssertionError(request.param)  # pragma: no cover

    return build


def gaps(stamps: list[float]) -> list[float]:
    ordered = sorted(stamps)
    return [b - a for a, b in zip(ordered, ordered[1:])]


def test_spacing_holds_across_threads(make_limiter) -> None:
    limiter = make_limiter()
    stamps: list[float] = []
    lock = threading.Lock()

    def hammer() -> None:
        for _ in range(4):
            limiter.wait()
            with lock:
                stamps.append(time.monotonic())

    threads = [threading.Thread(target=hammer) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(stamps) == 12
    # Wake-up jitter can shave a single gap, but never the overall rate:
    # an unshared limiter would let the three threads run side by side.
    assert min(gaps(stamps)) >= INTERVAL * 0.5
    assert max(stamps) - min(stamps) >= INTERVAL * 11 * 0.9


def test_zero_interval_never_sleeps(make_limiter) -> None:
    limiter = make_limiter(0.0)
    started = time.monotonic()
    for _ in range(20):
        limiter.wait()
    assert time.monotonic() - started < 0.5
