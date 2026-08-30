"""A durable job queue in SQLite that behaves like SQS.

Messages survive restarts, several worker processes can share one database
file, and the receive path is a single ``BEGIN IMMEDIATE`` transaction so two
workers can never lease the same message at once.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .queue import Message, QueueDepth

UTC = timezone.utc

SCHEMA = """
CREATE TABLE IF NOT EXISTS queue_messages (
    id            TEXT PRIMARY KEY,
    body          TEXT NOT NULL,
    sent_at       REAL NOT NULL,
    visible_at    REAL NOT NULL,
    receive_count INTEGER NOT NULL DEFAULT 0,
    receipt       TEXT,
    dead          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_queue_ready ON queue_messages(dead, visible_at, sent_at);
"""

POLL_INTERVAL_S = 0.1


class SqliteJobQueue:
    def __init__(self, path: Path, visibility_s: float = 120, max_receives: int = 5) -> None:
        self.path = path
        self.visibility_s = visibility_s
        self.max_receives = max_receives
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(path), check_same_thread=False, isolation_level=None, timeout=30
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def send(self, body: dict[str, Any], delay_s: int = 0) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO queue_messages (id, body, sent_at, visible_at) VALUES (?,?,?,?)",
                (uuid.uuid4().hex, json.dumps(body), now, now + delay_s),
            )

    def receive(self, max_messages: int = 1, wait_s: float = 0.0) -> list[Message]:
        deadline = time.monotonic() + wait_s
        while True:
            messages = self._receive_once(max_messages)
            if messages or time.monotonic() >= deadline:
                return messages
            time.sleep(min(POLL_INTERVAL_S, max(0.0, deadline - time.monotonic())))

    def _receive_once(self, max_messages: int) -> list[Message]:
        now = time.time()
        out: list[Message] = []
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                rows = self._conn.execute(
                    "SELECT * FROM queue_messages WHERE dead = 0 AND visible_at <= ? "
                    "ORDER BY sent_at LIMIT ?",
                    (now, max_messages),
                ).fetchall()
                for row in rows:
                    if row["receive_count"] >= self.max_receives:
                        # Received as often as allowed and still not done:
                        # park it instead of letting it poison the queue.
                        self._conn.execute(
                            "UPDATE queue_messages SET dead = 1 WHERE id = ?", (row["id"],)
                        )
                        continue
                    receipt = uuid.uuid4().hex
                    count = row["receive_count"] + 1
                    self._conn.execute(
                        "UPDATE queue_messages SET receive_count = ?, receipt = ?, "
                        "visible_at = ? WHERE id = ?",
                        (count, receipt, now + self.visibility_s, row["id"]),
                    )
                    out.append(self._message(row, count, receipt))
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return out

    def ack(self, message: Message) -> None:
        # Matching on the receipt means a worker whose lease already lapsed
        # cannot delete a message that another worker is now holding.
        with self._lock:
            self._conn.execute(
                "DELETE FROM queue_messages WHERE id = ? AND receipt = ?",
                (message.id, message.receipt),
            )

    def retry_later(self, message: Message, delay_s: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE queue_messages SET visible_at = ? WHERE id = ? AND receipt = ?",
                (time.time() + delay_s, message.id, message.receipt),
            )

    def depth(self) -> QueueDepth:
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT "
                "SUM(dead = 0 AND visible_at <= ?) AS visible, "
                "SUM(dead = 0 AND visible_at > ? AND receive_count > 0) AS in_flight, "
                "SUM(dead = 1) AS dead FROM queue_messages",
                (now, now),
            ).fetchone()
        return QueueDepth(
            visible=int(row["visible"] or 0),
            in_flight=int(row["in_flight"] or 0),
            dead=int(row["dead"] or 0),
        )

    def dead_letters(self, limit: int = 20) -> list[Message]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM queue_messages WHERE dead = 1 ORDER BY sent_at LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._message(r, r["receive_count"], r["receipt"] or "") for r in rows]

    @staticmethod
    def _message(row: sqlite3.Row, receive_count: int, receipt: str) -> Message:
        return Message(
            id=row["id"],
            body=json.loads(row["body"]),
            receive_count=receive_count,
            receipt=receipt,
            sent_at=datetime.fromtimestamp(row["sent_at"], UTC),
        )
