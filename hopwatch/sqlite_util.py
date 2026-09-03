"""Opening a SQLite file that several processes share.

Worker processes started together all open the same fresh database. Switching
a new file into WAL mode answers "database is locked" to a concurrent opener
straight away, without consulting the busy timeout, so each opener retries
that one statement itself.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Protocol

BUSY_TIMEOUT_S = 30.0


class _Executes(Protocol):
    def execute(self, sql: str) -> Any: ...


def enable_wal(conn: _Executes, attempts: int = 100, delay_s: float = 0.05) -> None:
    for attempt in range(1, attempts + 1):
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or attempt == attempts:
                raise
            time.sleep(delay_s)


def connect(path: Path, *, autocommit: bool = False) -> sqlite3.Connection:
    """A thread-shareable connection in WAL mode with a generous busy timeout."""
    path.parent.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, Any] = {"check_same_thread": False, "timeout": BUSY_TIMEOUT_S}
    if autocommit:
        kwargs["isolation_level"] = None
    conn = sqlite3.connect(str(path), **kwargs)
    conn.row_factory = sqlite3.Row
    enable_wal(conn)
    return conn
