"""Spacing between calls to Wizz Air, shared by everything that makes them.

The politeness budget is global: one request every ``MIN_REQUEST_INTERVAL``
seconds from this deployment, not from each worker. A limiter that lived in
each process would let five workers send five times the traffic -- exactly the
pattern that gets a Multipass account noticed. So the limiter that workers use
keeps its state somewhere they all see.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Protocol

from .sqlite_util import connect


class RateLimiter(Protocol):
    def wait(self) -> float:
        """Block until this caller may make one request; return the slot granted."""


class MemoryRateLimiter:
    """Minimum spacing between calls within one process, safe across threads."""

    def __init__(self, min_interval: float) -> None:
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> float:
        with self._lock:
            delta = time.monotonic() - self._last
            if delta < self._min_interval:
                time.sleep(self._min_interval - delta)
            self._last = time.monotonic()
            return self._last


class SqliteRateLimiter:
    """Minimum spacing between calls across every process sharing one file.

    Each caller reserves the next free slot in a single write transaction and
    then sleeps until it arrives, outside the lock. Reservations are strictly
    ordered, so N processes get one call per interval between them.
    """

    def __init__(self, path: Path, min_interval: float, name: str = "wizz") -> None:
        self.min_interval = min_interval
        self.name = name
        self._lock = threading.Lock()
        self._conn = connect(path, autocommit=True)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS rate_limit (name TEXT PRIMARY KEY, next_slot REAL NOT NULL)"
        )

    def wait(self) -> float:
        if self.min_interval <= 0:
            return time.time()
        slot = self._reserve()
        delay = slot - time.time()
        if delay > 0:
            time.sleep(delay)
        return slot

    def _reserve(self) -> float:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                now = time.time()
                row = self._conn.execute(
                    "SELECT next_slot FROM rate_limit WHERE name = ?", (self.name,)
                ).fetchone()
                slot = max(now, row[0]) if row else now
                self._conn.execute(
                    "INSERT INTO rate_limit (name, next_slot) VALUES (?, ?) "
                    "ON CONFLICT(name) DO UPDATE SET next_slot = excluded.next_slot",
                    (self.name, slot + self.min_interval),
                )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return slot

    def close(self) -> None:
        with self._lock:
            self._conn.close()
