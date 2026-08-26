"""Records and status values shared by every store implementation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Mapping

UTC = timezone.utc

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
    def from_row(cls, row: Mapping[str, Any]) -> Want:
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
    def from_row(cls, row: Mapping[str, Any]) -> Candidate:
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
    def from_row(cls, row: Mapping[str, Any]) -> Booking:
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


# Job lifecycle (layer-2 search jobs on the queue)
JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_DONE = "done"
JOB_FAILED = "failed"  # permanent: retrying cannot help
JOB_RETRYING = "retrying"
JOB_DEAD = "dead"  # gave up after max_receives; the message is dead-lettered

JOB_FINISHED_STATES = (JOB_DONE, JOB_FAILED, JOB_DEAD)

# Search run outcome
RUN_RUNNING = "running"
RUN_OK = "ok"
RUN_INCOMPLETE = "incomplete"  # some legs could not be checked
RUN_ERROR = "error"


@dataclass
class Job:
    key: str
    kind: str
    want_id: int
    status: str
    attempts: int
    worker: str | None
    lease_until: datetime | None
    last_error: str | None
    result: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Job:
        return cls(
            key=row["key"],
            kind=row["kind"],
            want_id=int(row["want_id"]),
            status=row["status"],
            attempts=int(row["attempts"]),
            worker=row["worker"],
            lease_until=_dt(row["lease_until"]),
            last_error=row["last_error"],
            result=json.loads(row["result_json"] or "{}"),
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
            finished_at=_dt(row["finished_at"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kind": self.kind,
            "want_id": self.want_id,
            "status": self.status,
            "attempts": self.attempts,
            "worker": self.worker,
            "lease_until": self.lease_until.isoformat() if self.lease_until else None,
            "last_error": self.last_error,
            "result": self.result,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


@dataclass
class SearchRun:
    """One attempt at sweeping one want: what it cost and what it found."""

    id: str
    job_key: str | None
    want_id: int
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    status: str
    paths_considered: int = 0
    routes_queried: int = 0
    upstream_calls: int = 0
    cache_hits: int = 0
    itineraries: int = 0
    new_candidates: int = 0
    failed_routes: list[str] = field(default_factory=list)

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> SearchRun:
        return cls(
            id=row["id"],
            job_key=row["job_key"],
            want_id=int(row["want_id"]),
            started_at=_dt(row["started_at"]),
            finished_at=_dt(row["finished_at"]),
            duration_ms=None if row["duration_ms"] is None else int(row["duration_ms"]),
            status=row["status"],
            paths_considered=int(row["paths_considered"]),
            routes_queried=int(row["routes_queried"]),
            upstream_calls=int(row["upstream_calls"]),
            cache_hits=int(row["cache_hits"]),
            itineraries=int(row["itineraries"]),
            new_candidates=int(row["new_candidates"]),
            failed_routes=json.loads(row["failed_routes_json"] or "[]"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "job_key": self.job_key,
            "want_id": self.want_id,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "paths_considered": self.paths_considered,
            "routes_queried": self.routes_queried,
            "upstream_calls": self.upstream_calls,
            "cache_hits": self.cache_hits,
            "itineraries": self.itineraries,
            "new_candidates": self.new_candidates,
            "failed_routes": self.failed_routes,
        }
