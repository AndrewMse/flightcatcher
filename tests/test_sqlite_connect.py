"""Opening a shared SQLite file while another process holds it locked."""

from __future__ import annotations

import sqlite3
import threading

import pytest

from hopwatch.jobs.sqlite_queue import SqliteJobQueue
from hopwatch.ratelimit import SqliteRateLimiter
from hopwatch.sqlite_util import enable_wal
from hopwatch.store import SqliteStore


def hold_lock(path, seconds: float) -> threading.Event:
    """Keep an exclusive lock on ``path`` for a while, from another connection."""
    ready = threading.Event()

    def run() -> None:
        conn = sqlite3.connect(str(path), isolation_level=None)
        conn.execute("BEGIN EXCLUSIVE")
        ready.set()
        threading.Event().wait(seconds)
        conn.execute("COMMIT")
        conn.close()

    threading.Thread(target=run, daemon=True).start()
    ready.wait(5)
    return ready


def test_limiter_opens_while_another_process_holds_the_file(tmp_path) -> None:
    path = tmp_path / "shared.db"
    sqlite3.connect(str(path)).close()
    hold_lock(path, 0.5)
    SqliteRateLimiter(path, 0.1).close()


def test_queue_opens_while_another_process_holds_the_file(tmp_path) -> None:
    path = tmp_path / "shared.db"
    sqlite3.connect(str(path)).close()
    hold_lock(path, 0.5)
    SqliteJobQueue(path).close()


def test_store_opens_while_another_process_holds_the_file(tmp_path) -> None:
    path = tmp_path / "shared.db"
    sqlite3.connect(str(path)).close()
    hold_lock(path, 0.5)
    SqliteStore(path).close()



class FlakyConnection:
    """Answers "database is locked" a few times, like a concurrent opener does."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def execute(self, sql: str):
        self.calls += 1
        if self.calls <= self.failures:
            raise sqlite3.OperationalError("database is locked")
        return None


def test_enable_wal_retries_a_locked_database() -> None:
    conn = FlakyConnection(failures=3)
    enable_wal(conn, attempts=10, delay_s=0)
    assert conn.calls == 4


def test_enable_wal_gives_up_eventually() -> None:
    conn = FlakyConnection(failures=100)
    with pytest.raises(sqlite3.OperationalError):
        enable_wal(conn, attempts=5, delay_s=0)
    assert conn.calls == 5


def test_enable_wal_does_not_swallow_other_errors() -> None:
    class Broken:
        def execute(self, sql: str):
            raise sqlite3.OperationalError("disk I/O error")

    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        enable_wal(Broken(), attempts=5, delay_s=0)


RACE_CHILD = """
import sys
from pathlib import Path
from hopwatch.ratelimit import SqliteRateLimiter
SqliteRateLimiter(Path(sys.argv[1]), 0.01).wait()
"""


def test_many_processes_can_create_the_same_file_at_once(tmp_path) -> None:
    """Workers started together all open a fresh database; none may crash."""
    import subprocess
    import sys

    for trial in range(6):
        path = tmp_path / f"fresh{trial}.db"
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", RACE_CHILD, str(path)],
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(6)
        ]
        for proc in procs:
            _, err = proc.communicate(timeout=60)
            assert proc.returncode == 0, err.strip().splitlines()[-1]
