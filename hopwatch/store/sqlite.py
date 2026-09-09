"""Persistent state, in SQLite.

Everything the watcher knows lives here so that a restart -- or a crash at 04:00
while a booking window is open -- loses nothing. Five tables:

* ``wants``      standing "get me from A to B" requests
* ``candidates`` itineraries discovered for a want, with their 72h windows
* ``checks``     results of authenticated Multipass availability checks
* ``bookings``   booking requests and their approval state
* ``events``     an activity feed for the UI
* ``jobs``       queued search jobs, keyed by their idempotency key
* ``search_runs`` what each sweep of a want cost and found
* ``heartbeats`` last sign of life from the booker and each worker
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..sqlite_util import connect
from .records import (
    BOOKED,
    EXPIRED,
    HOLD_EXPIRED,
    JOB_QUEUED,
    JOB_RETRYING,
    JOB_RUNNING,
    OPEN_BOOKING_STATES,
    RUN_RUNNING,
    PENDING_APPROVAL,
    PREPARING,
    UNKNOWN,
    UTC,
    WATCHING,
    Booking,
    Candidate,
    Job,
    SearchRun,
    Want,
    _dt,
    _now,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS wants (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    name                 TEXT NOT NULL,
    origin               TEXT NOT NULL,
    destination          TEXT NOT NULL,
    date_from            TEXT NOT NULL,
    date_to              TEXT NOT NULL,
    max_stops            INTEGER NOT NULL DEFAULT 1,
    min_layover_min      INTEGER NOT NULL DEFAULT 180,
    max_layover_min      INTEGER NOT NULL DEFAULT 1200,
    max_detour           REAL    NOT NULL DEFAULT 2.2,
    max_trip_hours       REAL    NOT NULL DEFAULT 30.0,
    allow_ground_transfer INTEGER NOT NULL DEFAULT 0,
    after_hour           INTEGER,
    before_hour          INTEGER,
    auto_request_booking INTEGER NOT NULL DEFAULT 0,
    active               INTEGER NOT NULL DEFAULT 1,
    notes                TEXT NOT NULL DEFAULT '',
    created_at           TEXT NOT NULL,
    last_searched_at     TEXT
);

CREATE TABLE IF NOT EXISTS candidates (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    want_id            INTEGER NOT NULL REFERENCES wants(id) ON DELETE CASCADE,
    signature          TEXT NOT NULL,
    path               TEXT NOT NULL,
    legs_json          TEXT NOT NULL,
    stops              INTEGER NOT NULL,
    total_minutes      INTEGER NOT NULL,
    ground_transfer    INTEGER NOT NULL DEFAULT 0,
    staggered_hours    REAL NOT NULL DEFAULT 0,
    departs_utc        TEXT NOT NULL,
    window_opens_utc   TEXT NOT NULL,
    window_closes_utc  TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'watching',
    availability       TEXT NOT NULL DEFAULT 'unknown',
    first_seen         TEXT NOT NULL,
    last_seen          TEXT NOT NULL,
    last_checked_at    TEXT,
    next_check_at      TEXT,
    check_count        INTEGER NOT NULL DEFAULT 0,
    alerted_at         TEXT,
    UNIQUE (want_id, signature)
);
CREATE INDEX IF NOT EXISTS idx_candidates_due ON candidates(status, next_check_at);

CREATE TABLE IF NOT EXISTS checks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    checked_at   TEXT NOT NULL,
    result       TEXT NOT NULL,
    detail_json  TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_checks_candidate ON checks(candidate_id, checked_at);

CREATE TABLE IF NOT EXISTS bookings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id    INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    created_at      TEXT NOT NULL,
    status          TEXT NOT NULL,
    summary         TEXT NOT NULL DEFAULT '',
    detail_json     TEXT NOT NULL DEFAULT '{}',
    screenshot_path TEXT,
    hold_expires_at TEXT,
    decided_at      TEXT,
    decided_by      TEXT,
    confirmed_at    TEXT,
    confirmation    TEXT,
    error           TEXT
);
CREATE INDEX IF NOT EXISTS idx_bookings_status ON bookings(status, created_at);

CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    level     TEXT NOT NULL DEFAULT 'info',
    kind      TEXT NOT NULL,
    message   TEXT NOT NULL,
    data_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC);

CREATE TABLE IF NOT EXISTS jobs (
    key          TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    want_id      INTEGER NOT NULL,
    status       TEXT NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    worker       TEXT,
    lease_until  TEXT,
    last_error   TEXT,
    result_json  TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    finished_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_jobs_finished ON jobs(finished_at);

CREATE TABLE IF NOT EXISTS search_runs (
    id                 TEXT PRIMARY KEY,
    job_key            TEXT,
    want_id            INTEGER NOT NULL,
    started_at         TEXT NOT NULL,
    finished_at        TEXT,
    duration_ms        INTEGER,
    status             TEXT NOT NULL,
    paths_considered   INTEGER NOT NULL DEFAULT 0,
    routes_queried     INTEGER NOT NULL DEFAULT 0,
    upstream_calls     INTEGER NOT NULL DEFAULT 0,
    cache_hits         INTEGER NOT NULL DEFAULT 0,
    itineraries        INTEGER NOT NULL DEFAULT 0,
    new_candidates     INTEGER NOT NULL DEFAULT 0,
    failed_routes_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_runs_started ON search_runs(started_at DESC);

CREATE TABLE IF NOT EXISTS heartbeats (
    component TEXT PRIMARY KEY,
    ts        TEXT NOT NULL
);
"""


class SqliteStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._conn = connect(path)
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _write(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    def _read(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    # --- wants --------------------------------------------------------------

    def add_want(self, **fields: Any) -> int:
        columns = [
            "name", "origin", "destination", "date_from", "date_to", "max_stops",
            "min_layover_min", "max_layover_min", "max_detour", "max_trip_hours",
            "allow_ground_transfer", "after_hour", "before_hour",
            "auto_request_booking", "active", "notes",
        ]
        values = [fields.get(c) for c in columns]
        placeholders = ", ".join("?" for _ in columns) + ", ?"
        cur = self._write(
            f"INSERT INTO wants ({', '.join(columns)}, created_at) VALUES ({placeholders})",
            values + [_now()],
        )
        return int(cur.lastrowid)

    def update_want(self, want_id: int, **fields: Any) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{k} = ?" for k in fields)
        self._write(
            f"UPDATE wants SET {assignments} WHERE id = ?",
            list(fields.values()) + [want_id],
        )

    def delete_want(self, want_id: int) -> None:
        self._write("DELETE FROM wants WHERE id = ?", (want_id,))

    def get_want(self, want_id: int) -> Want | None:
        rows = self._read("SELECT * FROM wants WHERE id = ?", (want_id,))
        return Want.from_row(rows[0]) if rows else None

    def list_wants(self, active_only: bool = False) -> list[Want]:
        sql = "SELECT * FROM wants"
        if active_only:
            sql += " WHERE active = 1"
        sql += " ORDER BY id"
        return [Want.from_row(r) for r in self._read(sql)]

    def mark_want_searched(self, want_id: int) -> None:
        self._write("UPDATE wants SET last_searched_at = ? WHERE id = ?", (_now(), want_id))

    # --- candidates ---------------------------------------------------------

    def upsert_candidate(
        self,
        want_id: int,
        signature: str,
        path: list[str],
        legs: list[dict[str, Any]],
        stops: int,
        total_minutes: int,
        ground_transfer: bool,
        staggered_hours: float,
        departs_utc: datetime,
        window_opens_utc: datetime,
        window_closes_utc: datetime,
    ) -> tuple[int, bool]:
        """Insert or refresh a candidate. Returns (id, was_new)."""
        now = _now()
        existing = self._read(
            "SELECT id FROM candidates WHERE want_id = ? AND signature = ?",
            (want_id, signature),
        )
        if existing:
            candidate_id = existing[0]["id"]
            self._write(
                "UPDATE candidates SET last_seen = ?, status = CASE WHEN status = ? "
                "THEN ? ELSE status END WHERE id = ?",
                (now, EXPIRED, WATCHING, candidate_id),
            )
            return candidate_id, False

        cur = self._write(
            """
            INSERT INTO candidates (
                want_id, signature, path, legs_json, stops, total_minutes,
                ground_transfer, staggered_hours, departs_utc, window_opens_utc,
                window_closes_utc, status, availability, first_seen, last_seen,
                next_check_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                want_id, signature, ">".join(path), json.dumps(legs), stops,
                total_minutes, int(ground_transfer), staggered_hours,
                departs_utc.isoformat(), window_opens_utc.isoformat(),
                window_closes_utc.isoformat(), WATCHING, UNKNOWN, now, now,
                window_opens_utc.isoformat(),
            ),
        )
        return int(cur.lastrowid), True

    def get_candidate(self, candidate_id: int) -> Candidate | None:
        rows = self._read(
            "SELECT c.*, w.name AS want_name FROM candidates c "
            "JOIN wants w ON w.id = c.want_id WHERE c.id = ?",
            (candidate_id,),
        )
        return Candidate.from_row(rows[0]) if rows else None

    def list_candidates(
        self,
        want_id: int | None = None,
        status: str | None = None,
        limit: int = 200,
    ) -> list[Candidate]:
        sql = (
            "SELECT c.*, w.name AS want_name FROM candidates c "
            "JOIN wants w ON w.id = c.want_id WHERE 1=1"
        )
        params: list[Any] = []
        if want_id is not None:
            sql += " AND c.want_id = ?"
            params.append(want_id)
        if status is not None:
            sql += " AND c.status = ?"
            params.append(status)
        sql += " ORDER BY c.window_opens_utc, c.departs_utc LIMIT ?"
        params.append(limit)
        return [Candidate.from_row(r) for r in self._read(sql, params)]

    def candidates_due_for_check(self, now: datetime, limit: int = 10) -> list[Candidate]:
        """Candidates whose window is open and whose next check is due."""
        stamp = now.isoformat()
        rows = self._read(
            """
            SELECT c.*, w.name AS want_name FROM candidates c
            JOIN wants w ON w.id = c.want_id
            WHERE c.status = ? AND w.active = 1
              AND c.window_opens_utc <= ? AND c.window_closes_utc > ?
              AND (c.next_check_at IS NULL OR c.next_check_at <= ?)
            ORDER BY c.window_closes_utc
            LIMIT ?
            """,
            (WATCHING, stamp, stamp, stamp, limit),
        )
        return [Candidate.from_row(r) for r in rows]

    def record_check(
        self,
        candidate_id: int,
        result: str,
        detail: dict[str, Any],
        next_check_at: datetime | None,
    ) -> None:
        now = _now()
        self._write(
            "INSERT INTO checks (candidate_id, checked_at, result, detail_json) "
            "VALUES (?,?,?,?)",
            (candidate_id, now, result, json.dumps(detail)),
        )
        self._write(
            "UPDATE candidates SET availability = ?, last_checked_at = ?, "
            "next_check_at = ?, check_count = check_count + 1 WHERE id = ?",
            (
                result,
                now,
                next_check_at.isoformat() if next_check_at else None,
                candidate_id,
            ),
        )

    def mark_alerted(self, candidate_id: int) -> None:
        self._write("UPDATE candidates SET alerted_at = ? WHERE id = ?", (_now(), candidate_id))

    def expire_stale_candidates(self, now: datetime) -> int:
        cur = self._write(
            "UPDATE candidates SET status = ? WHERE status = ? AND window_closes_utc <= ?",
            (EXPIRED, WATCHING, now.isoformat()),
        )
        return cur.rowcount

    # --- bookings -----------------------------------------------------------

    def create_booking(
        self, candidate_id: int, summary: str, detail: dict[str, Any]
    ) -> int:
        cur = self._write(
            "INSERT INTO bookings (candidate_id, created_at, status, summary, detail_json) "
            "VALUES (?,?,?,?,?)",
            (candidate_id, _now(), PREPARING, summary, json.dumps(detail)),
        )
        return int(cur.lastrowid)

    def update_booking(self, booking_id: int, **fields: Any) -> None:
        if "detail" in fields:
            fields["detail_json"] = json.dumps(fields.pop("detail"))
        for key in ("hold_expires_at", "decided_at", "confirmed_at"):
            if isinstance(fields.get(key), datetime):
                fields[key] = fields[key].isoformat()
        if not fields:
            return
        assignments = ", ".join(f"{k} = ?" for k in fields)
        self._write(
            f"UPDATE bookings SET {assignments} WHERE id = ?",
            list(fields.values()) + [booking_id],
        )

    def get_booking(self, booking_id: int) -> Booking | None:
        rows = self._read("SELECT * FROM bookings WHERE id = ?", (booking_id,))
        return Booking.from_row(rows[0]) if rows else None

    def list_bookings(self, statuses: Iterable[str] | None = None, limit: int = 100) -> list[Booking]:
        sql = "SELECT * FROM bookings"
        params: list[Any] = []
        if statuses:
            statuses = list(statuses)
            sql += f" WHERE status IN ({', '.join('?' for _ in statuses)})"
            params.extend(statuses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [Booking.from_row(r) for r in self._read(sql, params)]

    def has_open_booking(self, candidate_id: int) -> bool:
        rows = self._read(
            f"SELECT 1 FROM bookings WHERE candidate_id = ? AND status IN "
            f"({', '.join('?' for _ in OPEN_BOOKING_STATES)}) LIMIT 1",
            (candidate_id, *OPEN_BOOKING_STATES),
        )
        return bool(rows)

    def count_open_bookings(self) -> int:
        rows = self._read(
            f"SELECT COUNT(*) AS n FROM bookings WHERE status IN "
            f"({', '.join('?' for _ in OPEN_BOOKING_STATES)})",
            OPEN_BOOKING_STATES,
        )
        return int(rows[0]["n"])

    def count_bookings_since(self, since: datetime) -> int:
        rows = self._read(
            "SELECT COUNT(*) AS n FROM bookings WHERE status = ? AND confirmed_at >= ?",
            (BOOKED, since.isoformat()),
        )
        return int(rows[0]["n"])

    def expire_held_bookings(self, now: datetime) -> list[Booking]:
        rows = self._read(
            "SELECT * FROM bookings WHERE status = ? AND hold_expires_at IS NOT NULL "
            "AND hold_expires_at <= ?",
            (PENDING_APPROVAL, now.isoformat()),
        )
        for row in rows:
            self.update_booking(
                row["id"],
                status=HOLD_EXPIRED,
                error="Nobody approved before the held seat expired.",
            )
        return [Booking.from_row(r) for r in rows]

    # --- events -------------------------------------------------------------

    def log(
        self, kind: str, message: str, level: str = "info", **data: Any
    ) -> None:
        self._write(
            "INSERT INTO events (ts, level, kind, message, data_json) VALUES (?,?,?,?,?)",
            (_now(), level, kind, message, json.dumps(data)),
        )

    def list_events(self, limit: int = 100, since_id: int = 0) -> list[dict[str, Any]]:
        rows = self._read(
            "SELECT * FROM events WHERE id > ? ORDER BY id DESC LIMIT ?",
            (since_id, limit),
        )
        return [
            {
                "id": r["id"],
                "ts": r["ts"],
                "level": r["level"],
                "kind": r["kind"],
                "message": r["message"],
                "data": json.loads(r["data_json"]),
            }
            for r in rows
        ]

    def prune_events(self, keep_days: int = 30) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=keep_days)).isoformat()
        return self._write("DELETE FROM events WHERE ts < ?", (cutoff,)).rowcount

    # --- jobs ---------------------------------------------------------------

    def create_job(self, key: str, kind: str, want_id: int) -> bool:
        """Record a job unless its idempotency key already exists."""
        now = _now()
        cur = self._write(
            "INSERT INTO jobs (key, kind, want_id, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(key) DO NOTHING",
            (key, kind, want_id, JOB_QUEUED, now, now),
        )
        return cur.rowcount == 1

    def get_job(self, key: str) -> Job | None:
        rows = self._read("SELECT * FROM jobs WHERE key = ?", (key,))
        return Job.from_row(rows[0]) if rows else None

    def claim_job(
        self, key: str, worker: str, lease_s: float, now: datetime | None = None
    ) -> Job | None:
        """Take the job for ``worker``, or None if someone holds a live lease.

        A running job whose lease has lapsed belongs to a worker that died, so
        it can be taken over; that is how a crash turns into a retry.
        """
        now = now or datetime.now(UTC)
        stamp = now.isoformat()
        cur = self._write(
            "UPDATE jobs SET status = ?, worker = ?, lease_until = ?, "
            "attempts = attempts + 1, updated_at = ? "
            "WHERE key = ? AND (status IN (?, ?) OR (status = ? AND lease_until < ?))",
            (
                JOB_RUNNING, worker, (now + timedelta(seconds=lease_s)).isoformat(),
                stamp, key, JOB_QUEUED, JOB_RETRYING, JOB_RUNNING, stamp,
            ),
        )
        return self.get_job(key) if cur.rowcount == 1 else None

    def finish_job(
        self,
        key: str,
        status: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        now = _now()
        self._write(
            "UPDATE jobs SET status = ?, result_json = ?, last_error = COALESCE(?, last_error), "
            "lease_until = NULL, updated_at = ?, finished_at = ? WHERE key = ?",
            (status, json.dumps(result or {}), error, now, now, key),
        )

    def release_job(self, key: str, error: str) -> None:
        """Hand a job back for a later attempt after a transient failure."""
        self._write(
            "UPDATE jobs SET status = ?, last_error = ?, worker = NULL, lease_until = NULL, "
            "updated_at = ? WHERE key = ?",
            (JOB_RETRYING, error, _now(), key),
        )

    def touch_job(self, key: str, now: datetime | None = None) -> None:
        stamp = (now or datetime.now(UTC)).isoformat()
        self._write("UPDATE jobs SET updated_at = ? WHERE key = ?", (stamp, key))

    def list_jobs(self, status: str | None = None, limit: int = 100) -> list[Job]:
        sql = "SELECT * FROM jobs"
        params: list[Any] = []
        if status is not None:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(limit)
        return [Job.from_row(r) for r in self._read(sql, params)]

    def stale_jobs(
        self,
        older_than: datetime,
        statuses: Sequence[str] = (JOB_QUEUED, JOB_RETRYING),
        limit: int = 100,
    ) -> list[Job]:
        """Jobs waiting in ``statuses`` that nobody has touched since ``older_than``."""
        statuses = list(statuses)
        rows = self._read(
            f"SELECT * FROM jobs WHERE status IN ({', '.join('?' for _ in statuses)}) "
            "AND updated_at < ? ORDER BY updated_at LIMIT ?",
            (*statuses, older_than.isoformat(), limit),
        )
        return [Job.from_row(r) for r in rows]

    def job_outcomes_since(self, since: datetime) -> dict[str, int]:
        rows = self._read(
            "SELECT status, COUNT(*) AS n FROM jobs WHERE finished_at >= ? GROUP BY status",
            (since.isoformat(),),
        )
        return {r["status"]: int(r["n"]) for r in rows}

    # --- search runs --------------------------------------------------------

    def start_search_run(self, want_id: int, job_key: str | None) -> str:
        run_id = uuid.uuid4().hex
        self._write(
            "INSERT INTO search_runs (id, job_key, want_id, started_at, status) "
            "VALUES (?,?,?,?,?)",
            (run_id, job_key, want_id, _now(), RUN_RUNNING),
        )
        return run_id

    def finish_search_run(
        self,
        run_id: str,
        want_id: int,
        status: str,
        *,
        paths_considered: int = 0,
        routes_queried: int = 0,
        upstream_calls: int = 0,
        cache_hits: int = 0,
        itineraries: int = 0,
        new_candidates: int = 0,
        failed_routes: Sequence[str] = (),
    ) -> None:
        rows = self._read("SELECT started_at FROM search_runs WHERE id = ?", (run_id,))
        if not rows:
            return
        finished = datetime.now(UTC)
        duration_ms = int((finished - _dt(rows[0]["started_at"])).total_seconds() * 1000)
        self._write(
            "UPDATE search_runs SET finished_at = ?, duration_ms = ?, status = ?, "
            "paths_considered = ?, routes_queried = ?, upstream_calls = ?, cache_hits = ?, "
            "itineraries = ?, new_candidates = ?, failed_routes_json = ? WHERE id = ?",
            (
                finished.isoformat(), duration_ms, status, paths_considered,
                routes_queried, upstream_calls, cache_hits, itineraries,
                new_candidates, json.dumps(list(failed_routes)), run_id,
            ),
        )

    def list_search_runs(self, want_id: int | None = None, limit: int = 50) -> list[SearchRun]:
        sql = "SELECT * FROM search_runs"
        params: list[Any] = []
        if want_id is not None:
            sql += " WHERE want_id = ?"
            params.append(want_id)
        sql += " ORDER BY started_at DESC, rowid DESC LIMIT ?"
        params.append(limit)
        return [SearchRun.from_row(r) for r in self._read(sql, params)]

    # --- heartbeats ---------------------------------------------------------

    def beat(self, component: str, now: datetime | None = None) -> None:
        stamp = (now or datetime.now(UTC)).isoformat()
        self._write(
            "INSERT INTO heartbeats (component, ts) VALUES (?, ?) "
            "ON CONFLICT(component) DO UPDATE SET ts = excluded.ts",
            (component, stamp),
        )

    def heartbeats(self) -> dict[str, datetime]:
        return {r["component"]: _dt(r["ts"]) for r in self._read("SELECT * FROM heartbeats")}
