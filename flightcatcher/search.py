"""Itinerary construction and Multipass booking-window maths.

This is layer 2: it answers "could this trip physically work, and when does it
become buyable?" using only the open timetable endpoint. It says nothing about
whether a Multipass seat actually exists on a given flight -- that needs an
authenticated session and is layer 3's job.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Sequence

from . import config
from .client import WizzClient, WizzError
from .models import BookingWindow, Connection, Departure, Itinerary
from .network import RouteNetwork
from .timezones import tz_for

log = logging.getLogger(__name__)


@dataclass
class SearchOptions:
    date_from: date
    date_to: date
    max_stops: int = config.DEFAULT_MAX_STOPS
    min_layover_min: int = config.DEFAULT_MIN_LAYOVER_MIN
    max_layover_min: int = config.DEFAULT_MAX_LAYOVER_MIN
    max_detour: float = config.DEFAULT_MAX_DETOUR
    max_trip_hours: float = config.DEFAULT_MAX_TRIP_HOURS
    allow_ground_transfer: bool = False
    only_bookable_now: bool = False
    include_closed: bool = False
    earliest_departure_hour: int | None = None
    latest_departure_hour: int | None = None
    limit: int = 50
    refresh: bool = False
    sort_by: str = "window"  # "window" | "departure" | "duration"


@dataclass
class SearchResult:
    itineraries: list[Itinerary] = field(default_factory=list)
    paths_considered: int = 0
    routes_queried: int = 0
    departures_found: int = 0
    # Legs the backend would not answer for. Any itinerary through one of these
    # is missing from the results, so an empty list of itineraries alongside a
    # non-empty list of failures means "unknown", not "nothing".
    failed_routes: list[tuple[str, str]] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return not self.failed_routes


# --- timetable -> departures ------------------------------------------------


def _to_departures(
    net: RouteNetwork, flights: Iterable[dict], fallback_pair: tuple[str, str]
) -> list[Departure]:
    """Parse timetable rows into departures with UTC times.

    The endpoint resolves metro codes server-side, so a query for OTP can come
    back as BBU. The station the API reports is the one that matters, so it is
    what gets recorded -- the queried pair is only a fallback.
    """
    out: list[Departure] = []
    seen: set[tuple[str, str, str]] = set()

    for flight in flights:
        origin = (flight.get("departureStation") or fallback_pair[0]).strip().upper()
        dest = (flight.get("arrivalStation") or fallback_pair[1]).strip().upper()
        origin_ap = net.airport(origin)
        if origin_ap is None:
            continue

        zone = tz_for(origin, origin_ap.country_code)
        duration = net.duration_min(origin, dest)
        amount, currency = _price_of(flight)

        for raw in flight.get("departureDates") or []:
            key = (origin, dest, raw)
            if key in seen:
                continue
            seen.add(key)
            try:
                naive = datetime.fromisoformat(raw)
            except (TypeError, ValueError):
                continue
            local = naive.replace(tzinfo=zone)
            out.append(
                Departure(
                    origin=origin,
                    destination=dest,
                    departs_local=local,
                    departs_utc=local.astimezone(timezone.utc),
                    duration_min=duration,
                    price_amount=amount,
                    price_currency=currency,
                )
            )

    out.sort(key=lambda d: d.departs_utc)
    return out


def _price_of(flight: dict) -> tuple[float | None, str | None]:
    """Lowest advertised fare, or None when the API declines to quote one.

    ``priceType: "checkPrice"`` comes back with ``amount: 0``, which is a
    "ask us properly" marker rather than a free flight.
    """
    price = flight.get("price") or {}
    amount = price.get("amount")
    currency = price.get("currencyCode")
    if flight.get("priceType") == "checkPrice" or not amount:
        original = flight.get("originalPrice") or {}
        amount = original.get("amount") or None
        currency = original.get("currencyCode") or currency
    return (float(amount) if amount else None), currency


def fetch_departures(
    client: WizzClient,
    net: RouteNetwork,
    pairs: Sequence[tuple[str, str]],
    date_from: date,
    date_to: date,
    refresh: bool = False,
) -> tuple[dict[tuple[str, str], list[Departure]], list[tuple[str, str]]]:
    """Pull timetables for every route pair, a few at a time.

    Returns the departures found and the pairs that could not be checked at
    all. A leg that failed is reported rather than folded into "no flights".
    """

    def one(
        pair: tuple[str, str],
    ) -> tuple[tuple[str, str], list[Departure] | None]:
        origin, dest = pair
        try:
            flights = client.timetable(origin, dest, date_from, date_to, refresh=refresh)
        except WizzError as exc:
            log.warning("could not check %s->%s: %s", origin, dest, exc)
            return pair, None
        return pair, _to_departures(net, flights, pair)

    results: dict[tuple[str, str], list[Departure]] = {}
    failed: list[tuple[str, str]] = []
    if not pairs:
        return results, failed

    with ThreadPoolExecutor(max_workers=config.MAX_CONCURRENCY) as pool:
        for pair, departures in pool.map(one, pairs):
            if departures is None:
                failed.append(pair)
            else:
                results[pair] = departures
    return results, failed


# --- booking window ---------------------------------------------------------


def booking_window(itinerary: Itinerary, now: datetime | None = None) -> BookingWindow:
    """When the whole itinerary is simultaneously buyable on Multipass.

    Each leg is its own booking with its own 72h window, so the itinerary opens
    when the *last* leg opens and closes when the *first* leg's cutoff hits.
    Committing to leg 1 before the later legs unlock is how you end up holding
    a ticket to a connecting airport and nothing onward.
    """
    now = now or datetime.now(timezone.utc)
    window = timedelta(hours=config.MULTIPASS_WINDOW_HOURS)
    cutoff = timedelta(hours=config.MULTIPASS_CUTOFF_HOURS)

    opens = max(leg.departs_utc - window for leg in itinerary.legs)
    closes = min(leg.departs_utc - cutoff for leg in itinerary.legs)

    if now >= closes:
        status = "closed"
    elif now < opens:
        status = "too_early"
    else:
        status = "open"

    return BookingWindow(
        opens_utc=opens,
        closes_utc=closes,
        first_leg_opens_utc=itinerary.legs[0].departs_utc - window,
        status=status,
    )


# --- itinerary assembly -----------------------------------------------------


def _connection_between(
    net: RouteNetwork, arriving: Departure, departing: Departure
) -> Connection | None:
    """The gap between two legs, or None if they cannot be connected at all."""
    if not net.same_metro(arriving.destination, departing.origin):
        return None
    ground = arriving.destination != departing.origin
    gap = departing.departs_utc - arriving.arrives_utc
    return Connection(
        layover_min=int(gap.total_seconds() // 60),
        ground_transfer=ground,
        from_station=arriving.destination,
        to_station=departing.origin,
    )


def _layover_ok(connection: Connection, opts: SearchOptions) -> bool:
    minimum = opts.min_layover_min
    if connection.ground_transfer:
        if not opts.allow_ground_transfer:
            return False
        minimum += config.GROUND_TRANSFER_EXTRA_MIN
    return minimum <= connection.layover_min <= opts.max_layover_min


def _first_leg_ok(departure: Departure, opts: SearchOptions) -> bool:
    if not (opts.date_from <= departure.departs_local.date() <= opts.date_to):
        return False
    hour = departure.departs_local.hour
    if opts.earliest_departure_hour is not None and hour < opts.earliest_departure_hour:
        return False
    if opts.latest_departure_hour is not None and hour > opts.latest_departure_hour:
        return False
    return True


def build_itineraries(
    net: RouteNetwork,
    path: Sequence[str],
    departures: dict[tuple[str, str], list[Departure]],
    opts: SearchOptions,
) -> list[Itinerary]:
    """Every timing-feasible way to fly a given station path."""
    legs_by_hop = [
        departures.get((path[i], path[i + 1]), []) for i in range(len(path) - 1)
    ]
    if any(not options for options in legs_by_hop):
        return []

    max_trip = timedelta(hours=opts.max_trip_hours)
    found: list[Itinerary] = []

    def extend(chosen: list[Departure], connections: list[Connection]) -> None:
        depth = len(chosen)
        if depth == len(legs_by_hop):
            found.append(Itinerary(legs=list(chosen), connections=list(connections)))
            return

        previous = chosen[-1]
        for candidate in legs_by_hop[depth]:
            if candidate.departs_utc <= previous.arrives_utc:
                continue
            if candidate.departs_utc - chosen[0].departs_utc > max_trip:
                break  # departures are sorted, so everything later is worse too
            connection = _connection_between(net, previous, candidate)
            if connection is None or not _layover_ok(connection, opts):
                continue
            extend(chosen + [candidate], connections + [connection])

    for first in legs_by_hop[0]:
        if _first_leg_ok(first, opts):
            extend([first], [])

    return found


def search(
    client: WizzClient,
    net: RouteNetwork,
    origins: Sequence[str],
    destinations: Sequence[str],
    opts: SearchOptions,
    now: datetime | None = None,
) -> SearchResult:
    """Find every viable way to get from any origin to any destination."""
    now = now or datetime.now(timezone.utc)

    paths = net.find_paths(
        origins,
        destinations,
        max_stops=opts.max_stops,
        max_detour=opts.max_detour,
        on_or_after=opts.date_from,
    )
    if not paths:
        return SearchResult()

    pairs = sorted({(p[i], p[i + 1]) for p in paths for i in range(len(p) - 1)})

    # Connecting legs may depart a day or two after the first one, so the
    # timetable window has to reach past the requested departure range.
    departures, failed = fetch_departures(
        client,
        net,
        pairs,
        opts.date_from,
        opts.date_to + timedelta(days=config.CONNECTION_LOOKAHEAD_DAYS),
        refresh=opts.refresh,
    )

    itineraries: list[Itinerary] = []
    for path in paths:
        for itinerary in build_itineraries(net, path, departures, opts):
            itinerary.window = booking_window(itinerary, now=now)
            itineraries.append(itinerary)

    itineraries = [i for i in itineraries if i.window and i.window.is_viable]
    if not opts.include_closed:
        itineraries = [i for i in itineraries if i.window.status != "closed"]
    if opts.only_bookable_now:
        itineraries = [i for i in itineraries if i.window.status == "open"]

    itineraries = _dedupe(itineraries)
    itineraries.sort(key=_sort_key(opts.sort_by))

    return SearchResult(
        itineraries=itineraries[: opts.limit],
        paths_considered=len(paths),
        routes_queried=len(pairs),
        departures_found=sum(len(v) for v in departures.values()),
        failed_routes=failed,
    )


def _sort_key(sort_by: str):
    """Order results by whichever question the user is actually asking.

    ``window`` answers "what unlocks next"; ``departure`` answers "what leaves
    soonest"; ``duration`` answers "what is least painful".
    """
    if sort_by == "departure":
        return lambda i: (i.departs_utc, i.total_minutes, i.stops)
    if sort_by == "duration":
        return lambda i: (i.total_minutes, i.stops, i.departs_utc)
    return lambda i: (i.window.opens_utc, i.departs_utc, i.total_minutes, i.stops)


def _dedupe(itineraries: Iterable[Itinerary]) -> list[Itinerary]:
    """Collapse itineraries that are the same flights reached via different paths."""
    seen: dict[tuple, Itinerary] = {}
    for itinerary in itineraries:
        key = tuple(
            (leg.origin, leg.destination, leg.departs_utc.isoformat())
            for leg in itinerary.legs
        )
        seen.setdefault(key, itinerary)
    return list(seen.values())
