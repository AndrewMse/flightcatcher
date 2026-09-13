"""Persistent state in DynamoDB, behaving exactly like the SQLite store.

The contract tests run the same suite against both, so "the candidate exists"
or "the job already ran" means the same thing on a laptop and on AWS.

Items carry the same field names as the SQLite columns, so the shared record
classes read either. Three things SQL gave for free are built by hand here:

* **Integer ids.** Booking and candidate ids appear in URLs and Discord button
  ids, so they stay small integers, issued by an atomic counter in ``meta``.
* **Uniqueness.** ``(want_id, signature)`` is enforced with a marker item
  written in the same transaction as the candidate.
* **Cascades.** Deleting a want deletes its candidates, their checks and
  bookings, as the SQLite foreign keys do.

Small tables (wants, bookings) are scanned rather than indexed: at personal
scale they hold tens of items, and an index would cost more than it saves.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable, Iterator, Sequence

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from ..store.records import (
    BOOKED,
    EXPIRED,
    HOLD_EXPIRED,
    JOB_QUEUED,
    JOB_RETRYING,
    JOB_RUNNING,
    OPEN_BOOKING_STATES,
    PENDING_APPROVAL,
    PREPARING,
    RUN_RUNNING,
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
from .schema import TABLES, table_name

EVENTS_PK = "ev"
EVENT_TTL = timedelta(days=30)
JOB_TTL = timedelta(days=7)
RUN_TTL = timedelta(days=30)

WANT_FIELDS = (
    "name", "origin", "destination", "date_from", "date_to", "max_stops",
    "min_layover_min", "max_layover_min", "max_detour", "max_trip_hours",
    "allow_ground_transfer", "after_hour", "before_hour",
    "auto_request_booking", "active", "notes",
)
WANT_COLUMNS = ("id", *WANT_FIELDS, "created_at", "last_searched_at")
CANDIDATE_COLUMNS = (
    "id", "want_id", "signature", "path", "legs_json", "stops", "total_minutes",
    "ground_transfer", "staggered_hours", "departs_utc", "window_opens_utc",
    "window_closes_utc", "status", "availability", "first_seen", "last_seen",
    "last_checked_at", "next_check_at", "check_count", "alerted_at",
)
BOOKING_COLUMNS = (
    "id", "candidate_id", "created_at", "status", "summary", "detail_json",
    "screenshot_path", "hold_expires_at", "decided_at", "decided_by",
    "confirmed_at", "confirmation", "error",
)
JOB_COLUMNS = (
    "key", "kind", "want_id", "status", "attempts", "worker", "lease_until",
    "last_error", "result_json", "created_at", "updated_at", "finished_at",
)
RUN_COLUMNS = (
    "id", "job_key", "want_id", "started_at", "finished_at", "duration_ms",
    "status", "paths_considered", "routes_queried", "upstream_calls",
    "cache_hits", "itineraries", "new_candidates", "failed_routes_json",
)


def _plain(value: Any) -> Any:
    """DynamoDB numbers come back as Decimal; the records want int or float."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def _row(item: dict[str, Any], columns: Sequence[str], **extra: Any) -> dict[str, Any]:
    row = {column: _plain(item.get(column)) for column in columns}
    row.update(extra)
    return row


def _dynamo(value: Any) -> Any:
    """Python values to what boto3 will store: floats as Decimal."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _expires(delta: timedelta) -> int:
    return int(time.time() + delta.total_seconds())


def _window_key(opens: str, departs: str) -> str:
    return f"{opens}#{departs}"


class DynamoStore:
    def __init__(self, prefix: str, region: str, endpoint_url: str | None = None) -> None:
        self.prefix = prefix
        self._resource = boto3.resource(
            "dynamodb", region_name=region, endpoint_url=endpoint_url
        )
        # The resource's client converts plain Python values to DynamoDB's
        # typed form itself, transactions included.
        self._client = self._resource.meta.client
        self._tables = {
            name: self._resource.Table(table_name(prefix, name)) for name in TABLES
        }

    def close(self) -> None:
        return None

    # --- plumbing -----------------------------------------------------------

    def _t(self, name: str):
        return self._tables[name]

    def _name(self, name: str) -> str:
        return table_name(self.prefix, name)

    def _scan(self, table: str, **kwargs: Any) -> Iterator[dict[str, Any]]:
        while True:
            page = self._t(table).scan(**kwargs)
            yield from page.get("Items", [])
            if "LastEvaluatedKey" not in page:
                return
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def _query(self, table: str, **kwargs: Any) -> Iterator[dict[str, Any]]:
        while True:
            page = self._t(table).query(**kwargs)
            yield from page.get("Items", [])
            if "LastEvaluatedKey" not in page:
                return
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def _set(
        self,
        table: str,
        key: dict[str, Any],
        fields: dict[str, Any],
        condition: Any = None,
        remove: Iterable[str] = (),
    ) -> dict[str, Any]:
        """``SET a = :a, ...`` with names escaped, since many are reserved words."""
        names: dict[str, str] = {}
        values: dict[str, Any] = {}
        sets = []
        for i, (attr, value) in enumerate(fields.items()):
            names[f"#f{i}"] = attr
            values[f":v{i}"] = _dynamo(value)
            sets.append(f"#f{i} = :v{i}")
        clauses = []
        if sets:
            clauses.append("SET " + ", ".join(sets))
        removes = []
        for j, attr in enumerate(remove):
            names[f"#r{j}"] = attr
            removes.append(f"#r{j}")
        if removes:
            clauses.append("REMOVE " + ", ".join(removes))
        kwargs: dict[str, Any] = {
            "Key": key,
            "UpdateExpression": " ".join(clauses),
            "ExpressionAttributeNames": names,
            "ReturnValues": "ALL_NEW",
        }
        if values:
            kwargs["ExpressionAttributeValues"] = values
        if condition is not None:
            kwargs["ConditionExpression"] = condition
        return self._t(table).update_item(**kwargs)["Attributes"]

    def _next_id(self, entity: str) -> int:
        response = self._t("meta").update_item(
            Key={"pk": f"counter#{entity}"},
            UpdateExpression="ADD n :one",
            ExpressionAttributeValues={":one": 1},
            ReturnValues="UPDATED_NEW",
        )
        return int(response["Attributes"]["n"])

    @staticmethod
    def _conditional_failed(exc: ClientError) -> bool:
        return exc.response["Error"]["Code"] in (
            "ConditionalCheckFailedException",
            "TransactionCanceledException",
        )

    def _want_names(self) -> dict[int, str]:
        return {
            int(item["id"]): item["name"]
            for item in self._scan("wants", ProjectionExpression="id, #n",
                                   ExpressionAttributeNames={"#n": "name"})
        }

    def _candidate(self, item: dict[str, Any], names: dict[int, str]) -> Candidate:
        want_id = int(item["want_id"])
        return Candidate.from_row(
            _row(item, CANDIDATE_COLUMNS, want_name=names.get(want_id, ""))
        )

    # --- wants --------------------------------------------------------------

    def add_want(self, **fields: Any) -> int:
        want_id = self._next_id("wants")
        item = {column: _dynamo(fields.get(column)) for column in WANT_FIELDS}
        item.update(id=want_id, created_at=_now(), last_searched_at=None)
        self._t("wants").put_item(Item=item)
        return want_id

    def update_want(self, want_id: int, **fields: Any) -> None:
        if fields:
            self._set("wants", {"id": want_id}, fields)

    def delete_want(self, want_id: int) -> None:
        candidates = list(
            self._scan("candidates", FilterExpression=Attr("want_id").eq(want_id))
        )
        candidate_ids = {int(c["id"]) for c in candidates}
        for booking in self._scan("bookings"):
            if int(booking["candidate_id"]) in candidate_ids:
                self._t("bookings").delete_item(Key={"id": booking["id"]})
        for candidate in candidates:
            for check in self._query(
                "checks", KeyConditionExpression=Key("candidate_id").eq(candidate["id"])
            ):
                self._t("checks").delete_item(
                    Key={"candidate_id": check["candidate_id"], "checked_key": check["checked_key"]}
                )
            self._t("meta").delete_item(
                Key={"pk": f"cand#{want_id}#{candidate['signature']}"}
            )
            self._t("candidates").delete_item(Key={"id": candidate["id"]})
        self._t("wants").delete_item(Key={"id": want_id})

    def get_want(self, want_id: int) -> Want | None:
        item = self._t("wants").get_item(Key={"id": want_id}).get("Item")
        return Want.from_row(_row(item, WANT_COLUMNS)) if item else None

    def list_wants(self, active_only: bool = False) -> list[Want]:
        wants = [Want.from_row(_row(item, WANT_COLUMNS)) for item in self._scan("wants")]
        if active_only:
            wants = [w for w in wants if w.active]
        return sorted(wants, key=lambda w: w.id)

    def mark_want_searched(self, want_id: int) -> None:
        self._set("wants", {"id": want_id}, {"last_searched_at": _now()})

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
        marker = f"cand#{want_id}#{signature}"
        existing = self._t("meta").get_item(Key={"pk": marker}).get("Item")
        if existing is None:
            candidate_id = self._next_id("candidates")
            now = _now()
            opens, departs = window_opens_utc.isoformat(), departs_utc.isoformat()
            item = {
                "id": candidate_id, "want_id": want_id, "signature": signature,
                "path": ">".join(path), "legs_json": json.dumps(legs), "stops": stops,
                "total_minutes": total_minutes, "ground_transfer": int(ground_transfer),
                "staggered_hours": _dynamo(float(staggered_hours)), "departs_utc": departs,
                "window_opens_utc": opens, "window_closes_utc": window_closes_utc.isoformat(),
                "status": WATCHING, "availability": UNKNOWN, "first_seen": now,
                "last_seen": now, "next_check_at": opens, "check_count": 0,
                "window_key": _window_key(opens, departs),
            }
            try:
                self._client.transact_write_items(
                    TransactItems=[
                        {
                            "Put": {
                                "TableName": self._name("meta"),
                                "Item": {"pk": marker, "candidate_id": candidate_id},
                                "ConditionExpression": "attribute_not_exists(pk)",
                            }
                        },
                        {
                            "Put": {
                                "TableName": self._name("candidates"),
                                "Item": item,
                            }
                        },
                    ]
                )
                return candidate_id, True
            except ClientError as exc:
                if not self._conditional_failed(exc):
                    raise
                # Lost a race to another worker inserting the same itinerary.
                existing = self._t("meta").get_item(Key={"pk": marker}, ConsistentRead=True)["Item"]

        candidate_id = int(existing["candidate_id"])
        now = _now()
        try:
            self._set(
                "candidates",
                {"id": candidate_id},
                {"last_seen": now, "status": WATCHING},
                condition=Attr("status").eq(EXPIRED),
            )
        except ClientError as exc:
            if not self._conditional_failed(exc):
                raise
            self._set("candidates", {"id": candidate_id}, {"last_seen": now})
        return candidate_id, False

    def get_candidate(self, candidate_id: int) -> Candidate | None:
        item = self._t("candidates").get_item(Key={"id": candidate_id}).get("Item")
        if item is None:
            return None
        want = self._t("wants").get_item(Key={"id": item["want_id"]}).get("Item")
        names = {int(item["want_id"]): want["name"]} if want else {}
        return self._candidate(item, names)

    def list_candidates(
        self,
        want_id: int | None = None,
        status: str | None = None,
        limit: int = 200,
    ) -> list[Candidate]:
        names = self._want_names()
        if status is not None:
            items = self._query(
                "candidates",
                IndexName="by_status",
                KeyConditionExpression=Key("status").eq(status),
            )
        else:
            items = self._scan("candidates")
        candidates = [
            self._candidate(item, names)
            for item in items
            if int(item["want_id"]) in names
            and (want_id is None or int(item["want_id"]) == want_id)
        ]
        candidates.sort(key=lambda c: (c.window_opens_utc, c.departs_utc))
        return candidates[:limit]

    def candidates_due_for_check(self, now: datetime, limit: int = 10) -> list[Candidate]:
        """Candidates whose window is open and whose next check is due."""
        stamp = now.isoformat()
        active = {w.id: w.name for w in self.list_wants(active_only=True)}
        due = []
        for item in self._query(
            "candidates",
            IndexName="by_status",
            # Every key starting with an opening time <= now sorts below this.
            KeyConditionExpression=Key("status").eq(WATCHING)
            & Key("window_key").lte(f"{stamp}#￿"),
        ):
            if int(item["want_id"]) not in active:
                continue
            if item["window_opens_utc"] > stamp or item["window_closes_utc"] <= stamp:
                continue
            next_check = item.get("next_check_at")
            if next_check is not None and next_check > stamp:
                continue
            due.append(self._candidate(item, active))
        due.sort(key=lambda c: c.window_closes_utc)
        return due[:limit]

    def record_check(
        self,
        candidate_id: int,
        result: str,
        detail: dict[str, Any],
        next_check_at: datetime | None,
    ) -> None:
        now = _now()
        self._t("checks").put_item(
            Item={
                "candidate_id": candidate_id,
                "checked_key": f"{now}#{uuid.uuid4().hex[:8]}",
                "checked_at": now,
                "result": result,
                "detail_json": json.dumps(detail),
            }
        )
        self._t("candidates").update_item(
            Key={"id": candidate_id},
            UpdateExpression=(
                "SET availability = :r, last_checked_at = :now, next_check_at = :next "
                "ADD check_count :one"
            ),
            ExpressionAttributeValues={
                ":r": result,
                ":now": now,
                ":next": next_check_at.isoformat() if next_check_at else None,
                ":one": 1,
            },
        )

    def mark_alerted(self, candidate_id: int) -> None:
        self._set("candidates", {"id": candidate_id}, {"alerted_at": _now()})

    def expire_stale_candidates(self, now: datetime) -> int:
        stamp = now.isoformat()
        expired = 0
        for item in list(
            self._query(
                "candidates",
                IndexName="by_status",
                KeyConditionExpression=Key("status").eq(WATCHING),
            )
        ):
            if item["window_closes_utc"] <= stamp:
                self._set("candidates", {"id": item["id"]}, {"status": EXPIRED})
                expired += 1
        return expired

    # --- bookings -----------------------------------------------------------

    def create_booking(self, candidate_id: int, summary: str, detail: dict[str, Any]) -> int:
        booking_id = self._next_id("bookings")
        self._t("bookings").put_item(
            Item={
                "id": booking_id, "candidate_id": candidate_id, "created_at": _now(),
                "status": PREPARING, "summary": summary, "detail_json": json.dumps(detail),
            }
        )
        return booking_id

    def update_booking(self, booking_id: int, **fields: Any) -> None:
        if "detail" in fields:
            fields["detail_json"] = json.dumps(fields.pop("detail"))
        for key in ("hold_expires_at", "decided_at", "confirmed_at"):
            if isinstance(fields.get(key), datetime):
                fields[key] = fields[key].isoformat()
        if fields:
            self._set("bookings", {"id": booking_id}, fields)

    def _bookings(self) -> list[Booking]:
        return [Booking.from_row(_row(item, BOOKING_COLUMNS)) for item in self._scan("bookings")]

    def get_booking(self, booking_id: int) -> Booking | None:
        item = self._t("bookings").get_item(Key={"id": booking_id}).get("Item")
        return Booking.from_row(_row(item, BOOKING_COLUMNS)) if item else None

    def list_bookings(
        self, statuses: Iterable[str] | None = None, limit: int = 100
    ) -> list[Booking]:
        wanted = set(statuses) if statuses else None
        bookings = [b for b in self._bookings() if wanted is None or b.status in wanted]
        bookings.sort(key=lambda b: b.created_at, reverse=True)
        return bookings[:limit]

    def has_open_booking(self, candidate_id: int) -> bool:
        return any(
            b.candidate_id == candidate_id and b.status in OPEN_BOOKING_STATES
            for b in self._bookings()
        )

    def count_open_bookings(self) -> int:
        return sum(b.status in OPEN_BOOKING_STATES for b in self._bookings())

    def count_bookings_since(self, since: datetime) -> int:
        return sum(
            b.status == BOOKED and b.confirmed_at is not None and b.confirmed_at >= since
            for b in self._bookings()
        )

    def expire_held_bookings(self, now: datetime) -> list[Booking]:
        stale = [
            b
            for b in self._bookings()
            if b.status == PENDING_APPROVAL
            and b.hold_expires_at is not None
            and b.hold_expires_at <= now
        ]
        for booking in stale:
            self.update_booking(
                booking.id,
                status=HOLD_EXPIRED,
                error="Nobody approved before the held seat expired.",
            )
        return stale

    # --- events -------------------------------------------------------------

    def log(self, kind: str, message: str, level: str = "info", **data: Any) -> None:
        self._t("events").put_item(
            Item={
                "pk": EVENTS_PK, "id": self._next_id("events"), "ts": _now(),
                "level": level, "kind": kind, "message": message,
                "data_json": json.dumps(data), "expires_at": _expires(EVENT_TTL),
            }
        )

    def list_events(self, limit: int = 100, since_id: int = 0) -> list[dict[str, Any]]:
        page = self._t("events").query(
            KeyConditionExpression=Key("pk").eq(EVENTS_PK) & Key("id").gt(since_id),
            ScanIndexForward=False,
            Limit=limit,
        )
        return [
            {
                "id": int(item["id"]),
                "ts": item["ts"],
                "level": item["level"],
                "kind": item["kind"],
                "message": item["message"],
                "data": json.loads(item["data_json"]),
            }
            for item in page.get("Items", [])
        ]

    def prune_events(self, keep_days: int = 30) -> int:
        return 0  # the table's TTL does this

    # --- jobs ---------------------------------------------------------------

    def _job(self, item: dict[str, Any]) -> Job:
        return Job.from_row(_row(item, JOB_COLUMNS))

    def create_job(self, key: str, kind: str, want_id: int) -> bool:
        now = _now()
        try:
            self._t("jobs").put_item(
                Item={
                    "key": key, "kind": kind, "want_id": want_id, "status": JOB_QUEUED,
                    "attempts": 0, "result_json": "{}", "created_at": now,
                    "updated_at": now, "expires_at": _expires(JOB_TTL),
                },
                ConditionExpression="attribute_not_exists(#k)",
                ExpressionAttributeNames={"#k": "key"},
            )
        except ClientError as exc:
            if self._conditional_failed(exc):
                return False
            raise
        return True

    def get_job(self, key: str) -> Job | None:
        item = self._t("jobs").get_item(Key={"key": key}, ConsistentRead=True).get("Item")
        return self._job(item) if item else None

    def claim_job(
        self, key: str, worker: str, lease_s: float, now: datetime | None = None
    ) -> Job | None:
        now = now or datetime.now(UTC)
        stamp = now.isoformat()
        try:
            item = self._t("jobs").update_item(
                Key={"key": key},
                UpdateExpression=(
                    "SET #s = :running, worker = :w, lease_until = :lease, updated_at = :now "
                    "ADD attempts :one"
                ),
                ConditionExpression=(
                    "attribute_exists(#k) AND (#s IN (:queued, :retrying) "
                    "OR (#s = :running AND lease_until < :now))"
                ),
                ExpressionAttributeNames={"#s": "status", "#k": "key"},
                ExpressionAttributeValues={
                    ":running": JOB_RUNNING, ":queued": JOB_QUEUED,
                    ":retrying": JOB_RETRYING, ":w": worker, ":now": stamp,
                    ":lease": (now + timedelta(seconds=lease_s)).isoformat(), ":one": 1,
                },
                ReturnValues="ALL_NEW",
            )["Attributes"]
        except ClientError as exc:
            if self._conditional_failed(exc):
                return None
            raise
        return self._job(item)

    def finish_job(
        self,
        key: str,
        status: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        now = _now()
        fields: dict[str, Any] = {
            "status": status, "result_json": json.dumps(result or {}),
            "updated_at": now, "finished_at": now,
        }
        if error is not None:
            fields["last_error"] = error
        self._set("jobs", {"key": key}, fields, remove=("lease_until",))

    def release_job(self, key: str, error: str) -> None:
        self._set(
            "jobs",
            {"key": key},
            {"status": JOB_RETRYING, "last_error": error, "updated_at": _now()},
            remove=("worker", "lease_until"),
        )

    def touch_job(self, key: str, now: datetime | None = None) -> None:
        self._set("jobs", {"key": key}, {"updated_at": (now or datetime.now(UTC)).isoformat()})

    def list_jobs(self, status: str | None = None, limit: int = 100) -> list[Job]:
        if status is not None:
            items = self._query(
                "jobs",
                IndexName="by_status",
                KeyConditionExpression=Key("status").eq(status),
                ScanIndexForward=False,
            )
        else:
            items = self._scan("jobs")
        jobs = sorted((self._job(i) for i in items), key=lambda j: j.created_at, reverse=True)
        return jobs[:limit]

    def stale_jobs(
        self,
        older_than: datetime,
        statuses: Sequence[str] = (JOB_QUEUED, JOB_RETRYING),
        limit: int = 100,
    ) -> list[Job]:
        stamp = older_than.isoformat()
        stale = [
            self._job(item)
            for status in statuses
            for item in self._query(
                "jobs",
                IndexName="by_status",
                KeyConditionExpression=Key("status").eq(status),
            )
            if item["updated_at"] < stamp
        ]
        stale.sort(key=lambda j: j.updated_at)
        return stale[:limit]

    def job_outcomes_since(self, since: datetime) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self._scan("jobs", FilterExpression=Attr("finished_at").gte(since.isoformat())):
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        return counts

    # --- search runs --------------------------------------------------------

    def start_search_run(self, want_id: int, job_key: str | None) -> str:
        run_id = uuid.uuid4().hex
        self._t("search_runs").put_item(
            Item={
                "want_id": want_id, "id": run_id, "job_key": job_key,
                "started_at": _now(), "status": RUN_RUNNING, "paths_considered": 0,
                "routes_queried": 0, "upstream_calls": 0, "cache_hits": 0,
                "itineraries": 0, "new_candidates": 0, "failed_routes_json": "[]",
                "expires_at": _expires(RUN_TTL),
            }
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
        key = {"want_id": want_id, "id": run_id}
        item = self._t("search_runs").get_item(Key=key).get("Item")
        if item is None:
            return
        finished = datetime.now(UTC)
        self._set(
            "search_runs",
            key,
            {
                "finished_at": finished.isoformat(),
                "duration_ms": int((finished - _dt(item["started_at"])).total_seconds() * 1000),
                "status": status,
                "paths_considered": paths_considered,
                "routes_queried": routes_queried,
                "upstream_calls": upstream_calls,
                "cache_hits": cache_hits,
                "itineraries": itineraries,
                "new_candidates": new_candidates,
                "failed_routes_json": json.dumps(list(failed_routes)),
            },
        )

    def list_search_runs(self, want_id: int | None = None, limit: int = 50) -> list[SearchRun]:
        if want_id is not None:
            items = self._query("search_runs", KeyConditionExpression=Key("want_id").eq(want_id))
        else:
            items = self._scan("search_runs")
        runs = [SearchRun.from_row(_row(item, RUN_COLUMNS)) for item in items]
        runs.sort(key=lambda r: r.started_at, reverse=True)
        return runs[:limit]

    # --- heartbeats ---------------------------------------------------------

    def beat(self, component: str, now: datetime | None = None) -> None:
        stamp = (now or datetime.now(UTC)).isoformat()
        self._set("meta", {"pk": "heartbeats"}, {f"hb:{component}": stamp})

    def heartbeats(self) -> dict[str, datetime]:
        item = self._t("meta").get_item(Key={"pk": "heartbeats"}).get("Item") or {}
        return {
            attr[3:]: _dt(value) for attr, value in item.items() if attr.startswith("hb:")
        }
