"""Sweeping one want: layer-2 search, then persisting what it found.

This is the unit of work a queued job performs. It only touches the public
timetable endpoint and the store, never the browser, which is what makes it
safe to run on any worker, any number of times.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from .client import WizzClient
from .models import Itinerary
from .network import RouteNetwork
from .search import SearchOptions, search
from .store import Store, Want


class PermanentJobError(Exception):
    """A job that will fail the same way however often it is retried."""


@dataclass
class SweepResult:
    itineraries: int
    new_candidates: int
    complete: bool
    failed_routes: list[str] = field(default_factory=list)
    paths_considered: int = 0
    routes_queried: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def signature_for(itinerary: Itinerary) -> str:
    raw = "|".join(
        f"{leg.origin}>{leg.destination}@{leg.departs_utc.isoformat()}"
        for leg in itinerary.legs
    )
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def legs_payload(itinerary: Itinerary) -> list[dict[str, Any]]:
    return [
        {
            "origin": leg.origin,
            "destination": leg.destination,
            "departs_local": leg.departs_local.isoformat(),
            "departs_utc": leg.departs_utc.isoformat(),
            "arrives_utc_estimated": leg.arrives_utc.isoformat(),
            "duration_min_estimated": leg.duration_min,
        }
        for leg in itinerary.legs
    ]


def sweep_want(
    store: Store,
    client: WizzClient,
    network: RouteNetwork,
    want: Want,
    today: date | None = None,
) -> SweepResult:
    origins = network.resolve(want.origin)
    destinations = network.resolve(want.destination)
    if not origins or not destinations:
        message = (
            f"Want {want.name!r}: could not resolve {want.origin!r} or {want.destination!r}"
        )
        store.log("search", message, level="error", want_id=want.id)
        raise PermanentJobError(message)

    today = today or date.today()
    opts = SearchOptions(
        date_from=max(want.date_from, today),
        date_to=want.date_to,
        max_stops=want.max_stops,
        min_layover_min=want.min_layover_min,
        max_layover_min=want.max_layover_min,
        max_detour=want.max_detour,
        max_trip_hours=want.max_trip_hours,
        allow_ground_transfer=want.allow_ground_transfer,
        earliest_departure_hour=want.after_hour,
        latest_departure_hour=want.before_hour,
        limit=200,
    )
    if opts.date_to < opts.date_from:
        store.mark_want_searched(want.id)
        return SweepResult(itineraries=0, new_candidates=0, complete=True)

    result = search(client, network, origins, destinations, opts)

    failed = [f"{a}→{b}" for a, b in result.failed_routes]
    if failed:
        store.log(
            "search",
            f"Want {want.name!r}: incomplete, could not check {', '.join(failed)}",
            level="warning",
            want_id=want.id,
        )

    new = 0
    for itinerary in result.itineraries:
        window = itinerary.window
        if window is None or window.status == "closed":
            continue
        _, was_new = store.upsert_candidate(
            want_id=want.id,
            signature=signature_for(itinerary),
            path=itinerary.path,
            legs=legs_payload(itinerary),
            stops=itinerary.stops,
            total_minutes=itinerary.total_minutes,
            ground_transfer=itinerary.has_ground_transfer,
            staggered_hours=window.staggered_hours,
            departs_utc=itinerary.departs_utc,
            window_opens_utc=window.opens_utc,
            window_closes_utc=window.closes_utc,
        )
        new += int(was_new)

    store.mark_want_searched(want.id)
    if new:
        store.log(
            "search",
            f"Want {want.name!r}: {new} new candidate(s), {len(result.itineraries)} total",
            want_id=want.id,
        )
    return SweepResult(
        itineraries=len(result.itineraries),
        new_candidates=new,
        complete=not failed,
        failed_routes=failed,
        paths_considered=result.paths_considered,
        routes_queried=result.routes_queried,
    )
