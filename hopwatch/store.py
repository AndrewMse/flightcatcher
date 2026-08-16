"""Persistent state, in SQLite.

Everything the watcher knows lives here so that a restart -- or a crash at 04:00
while a booking window is open -- loses nothing. Five tables:

* ``wants``      standing "get me from A to B" requests
* ``candidates`` itineraries discovered for a want, with their 72h windows
* ``checks``     results of authenticated Multipass availability checks
* ``bookings``   booking requests and their approval state
* ``events``     an activity feed for the UI
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

UTC = timezone.utc

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
"""

# Candidate lifecycle
WATCHING = "watching"
EXPIRED = "expired"
# Availability, as last reported by an authenticated check
UNKNOWN = "unknown"
AVAILABLE = "available"
SOLD_OUT = "sold_out"
CHECK_FAILED = "check_failed"

# Booking lifecycle
PREPARING = "preparing"
PENDING_APPROVAL = "pending_approval"
APPROVED = "approved"
BOOKING = "booking"
BOOKED = "booked"
REJECTED = "rejected"
FAILED = "failed"
HOLD_EXPIRED = "hold_expired"

OPEN_BOOKING_STATES = (PREPARING, PENDING_APPROVAL, APPROVED, BOOKING)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass
class Want:
    id: int
    name: str
    origin: str
    destination: str
    date_from: date
    date_to: date
    max_stops: int = 1
    min_layover_min: int = 180
    max_layover_min: int = 1200
    max_detour: float = 2.2
    max_trip_hours: float = 30.0
    allow_ground_transfer: bool = False
    after_hour: int | None = None
    before_hour: int | None = None
    auto_request_booking: bool = False
    active: bool = True
    notes: str = ""
    created_at: datetime | None = None
    last_searched_at: datetime | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Want:
        return cls(
            id=row["id"],
            name=row["name"],
            origin=row["origin"],
            destination=row["destination"],
            date_from=date.fromisoformat(row["date_from"]),
            date_to=date.fromisoformat(row["date_to"]),
            max_stops=row["max_stops"],
            min_layover_min=row["min_layover_min"],
            max_layover_min=row["max_layover_min"],
            max_detour=row["max_detour"],
            max_trip_hours=row["max_trip_hours"],
            allow_ground_transfer=bool(row["allow_ground_transfer"]),
            after_hour=row["after_hour"],
            before_hour=row["before_hour"],
            auto_request_booking=bool(row["auto_request_booking"]),
            active=bool(row["active"]),
            notes=row["notes"],
            created_at=_dt(row["created_at"]),
            last_searched_at=_dt(row["last_searched_at"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "origin": self.origin,
            "destination": self.destination,
            "date_from": self.date_from.isoformat(),
            "date_to": self.date_to.isoformat(),
            "max_stops": self.max_stops,
            "min_layover_min": self.min_layover_min,
            "max_layover_min": self.max_layover_min,
            "max_detour": self.max_detour,
            "max_trip_hours": self.max_trip_hours,
            "allow_ground_transfer": self.allow_ground_transfer,
            "after_hour": self.after_hour,
            "before_hour": self.before_hour,
            "auto_request_booking": self.auto_request_booking,
            "active": self.active,
            "notes": self.notes,
            "last_searched_at": self.last_searched_at.isoformat()
            if self.last_searched_at
            else None,
        }


@dataclass
class Candidate:
    id: int
    want_id: int
    signature: str
    path: list[str]
    legs: list[dict[str, Any]]
    stops: int
    total_minutes: int
    ground_transfer: bool
    staggered_hours: float
    departs_utc: datetime
    window_opens_utc: datetime
    window_closes_utc: datetime
    status: str
    availability: str
    last_checked_at: datetime | None = None
    next_check_at: datetime | None = None
    check_count: int = 0
    alerted_at: datetime | None = None
    want_name: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Candidate:
        keys = row.keys()
        return cls(
            id=row["id"],
            want_id=row["want_id"],
            signature=row["signature"],
            path=row["path"].split(">"),
            legs=json.loads(row["legs_json"]),
            stops=row["stops"],
            total_minutes=row["total_minutes"],
            ground_transfer=bool(row["ground_transfer"]),
            staggered_hours=row["staggered_hours"],
            departs_utc=_dt(row["departs_utc"]),
            window_opens_utc=_dt(row["window_opens_utc"]),
            window_closes_utc=_dt(row["window_closes_utc"]),
            status=row["status"],
            availability=row["availability"],
            last_checked_at=_dt(row["last_checked_at"]),
            next_check_at=_dt(row["next_check_at"]),
            check_count=row["check_count"],
            alerted_at=_dt(row["alerted_at"]),
            want_name=row["want_name"] if "want_name" in keys else "",
        )

    def window_status(self, now: datetime | None = None) -> str:
        now = now or datetime.now(UTC)
        if now >= self.window_closes_utc:
            return "closed"
        if now < self.window_opens_utc:
            return "too_early"
        return "open"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "want_id": self.want_id,
            "want_name": self.want_name,
            "path": self.path,
            "legs": self.legs,
            "stops": self.stops,
            "total_minutes": self.total_minutes,
            "ground_transfer": self.ground_transfer,
            "staggered_hours": self.staggered_hours,
            "departs_utc": self.departs_utc.isoformat(),
            "window_opens_utc": self.window_opens_utc.isoformat(),
            "window_closes_utc": self.window_closes_utc.isoformat(),
            "window_status": self.window_status(),
            "status": self.status,
            "availability": self.availability,
            "last_checked_at": self.last_checked_at.isoformat()
            if self.last_checked_at
            else None,
            "check_count": self.check_count,
            "trip_credits": len(self.legs),
        }


@dataclass
class Booking:
    id: int
    candidate_id: int
    created_at: datetime
    status: str
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)
    screenshot_path: str | None = None
    hold_expires_at: datetime | None = None
    decided_at: datetime | None = None
    decided_by: str | None = None
    confirmed_at: datetime | None = None
    confirmation: str | None = None
    error: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Booking:
        return cls(
            id=row["id"],
            candidate_id=row["candidate_id"],
            created_at=_dt(row["created_at"]),
            status=row["status"],
            summary=row["summary"],
            detail=json.loads(row["detail_json"]),
            screenshot_path=row["screenshot_path"],
            hold_expires_at=_dt(row["hold_expires_at"]),
            decided_at=_dt(row["decided_at"]),
            decided_by=row["decided_by"],
            confirmed_at=_dt(row["confirmed_at"]),
            confirmation=row["confirmation"],
            error=row["error"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "candidate_id": self.candidate_id,
            "created_at": self.created_at.isoformat(),
            "status": self.status,
            "summary": self.summary,
            "detail": self.detail,
            "has_screenshot": bool(self.screenshot_path),
            "hold_expires_at": self.hold_expires_at.isoformat()
            if self.hold_expires_at
            else None,
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
            "decided_by": self.decided_by,
            "confirmed_at": self.confirmed_at.isoformat() if self.confirmed_at else None,
            "confirmation": self.confirmation,
            "error": self.error,
        }


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
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
