"""Layer 3: does a Multipass seat actually exist on this flight?

Layer 2 can only prove a flight is *scheduled*. Whether your pass can take a
seat on it is an account-level fact behind a WAF, so the only way to find out
is to ask as a logged-in browser.

Rather than scrape the rendered fare cards, this drives the page to the
flight-select step and reads the ``/Api/search/search`` response the app
fetches for itself. That response carries real inventory, fare bundles and --
usefully -- genuine arrival times, which the public timetable endpoint omits.

Every check keeps the raw fare JSON in its result. The exact shape of a
Multipass fare is the one thing here that cannot be confirmed without a live
pass, so ``hopwatch probe`` plus these stored payloads are how the
detection below gets calibrated rather than guessed at forever.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from . import selectors
from .browser import BrowserSession, ResponseCollector
from .store import AVAILABLE, CHECK_FAILED, SOLD_OUT, UNKNOWN
from .settings import WatcherSettings

log = logging.getLogger(__name__)
UTC = timezone.utc

# Strings that mark a fare as bookable on Multipass. Kept together and matched
# case-insensitively so recalibration is a one-line edit.
MULTIPASS_MARKERS = ("multipass", "multi_pass", "multi pass", "wizzmultipass")


class CheckBudget:
    """Hard cap on authenticated traffic.

    Unauthenticated timetable polling is cheap and boring. These checks are
    neither: they ride a logged-in session, and the account is what is at risk
    if this ever looks like a scrape. The budget is the safety rail.
    """

    def __init__(self, settings: WatcherSettings) -> None:
        self.max_per_hour = settings.max_checks_per_hour
        self.min_gap = settings.min_seconds_between_checks
        self._times: list[float] = []
        self._lock = asyncio.Lock()

    def _prune(self, now: float) -> None:
        self._times = [t for t in self._times if now - t < 3600]

    @property
    def remaining(self) -> int:
        self._prune(time.monotonic())
        return max(0, self.max_per_hour - len(self._times))

    def seconds_until_free(self) -> float:
        now = time.monotonic()
        self._prune(now)
        waits = [0.0]
        if self._times:
            waits.append(self.min_gap - (now - self._times[-1]))
        if len(self._times) >= self.max_per_hour:
            waits.append(3600 - (now - self._times[0]))
        return max(waits)

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                wait = self.seconds_until_free()
                if wait <= 0:
                    self._times.append(time.monotonic())
                    return
            log.debug("check budget exhausted, sleeping %.0fs", wait)
            await asyncio.sleep(min(wait, 60))


@dataclass
class LegAvailability:
    origin: str
    destination: str
    departs_local: str
    found: bool = False
    multipass: bool | None = None
    seats: int | None = None
    flight_number: str | None = None
    arrives_local_actual: str | None = None
    signal: str = "none"
    fares: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "origin": self.origin,
            "destination": self.destination,
            "departs_local": self.departs_local,
            "found": self.found,
            "multipass": self.multipass,
            "seats": self.seats,
            "flight_number": self.flight_number,
            "arrives_local_actual": self.arrives_local_actual,
            "signal": self.signal,
            "fares": self.fares,
            "error": self.error,
        }


@dataclass
class ItineraryAvailability:
    result: str
    legs: list[LegAvailability] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "result": self.result,
            "note": self.note,
            "legs": [leg.to_dict() for leg in self.legs],
        }


# --- response parsing -------------------------------------------------------


def _iso_minute(value: str | None) -> str | None:
    """Normalise a datetime string to 'YYYY-MM-DDTHH:MM' for comparison."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime(
            "%Y-%m-%dT%H:%M"
        )
    except ValueError:
        return value[:16] if len(value) >= 16 else None


def _fare_is_multipass(fare: dict[str, Any]) -> bool:
    """Does this fare represent a Multipass seat?

    Matched across every plausible field rather than one known key, because the
    exact shape is unconfirmed. Over-matching here is the safer error: a false
    positive surfaces a booking for you to approve and reject, a false negative
    silently loses the window.
    """
    haystack = " ".join(
        str(fare.get(key, ""))
        for key in ("bundle", "bundleCode", "fareType", "name", "type",
                    "productClass", "fareSellKey", "priceType", "discountType")
    ).lower()
    return any(marker in haystack for marker in MULTIPASS_MARKERS)


def _seat_count(flight: dict[str, Any], fares: Sequence[dict[str, Any]]) -> int | None:
    for key in ("availableCount", "availableSeatCount", "seatAvailability"):
        value = flight.get(key)
        if isinstance(value, int):
            return value
    for fare in fares:
        for key in ("availableCount", "availableSeatCount"):
            value = fare.get(key)
            if isinstance(value, int):
                return value
    return None


def parse_leg(
    body: Any,
    origin: str,
    destination: str,
    departs_local: str,
) -> LegAvailability:
    """Pick our flight out of a search response and judge its Multipass status."""
    leg = LegAvailability(
        origin=origin, destination=destination, departs_local=departs_local
    )
    if not isinstance(body, dict):
        leg.error = "search response was not an object"
        return leg

    # An empty flight list is a real answer ("nothing on sale"). A response with
    # no flight-list key at all is a broken answer. Conflating them would retire
    # a live candidate as sold out instead of retrying it.
    if "outboundFlights" in body:
        flights = body["outboundFlights"]
    elif "flights" in body:
        flights = body["flights"]
    else:
        leg.error = "search response contained no flight list"
        return leg

    if not isinstance(flights, list):
        leg.error = "search response flight list was not a list"
        return leg

    wanted = _iso_minute(departs_local)
    match: dict[str, Any] | None = None
    for flight in flights:
        if not isinstance(flight, dict):
            continue
        candidate_times = [
            _iso_minute(flight.get(key))
            for key in ("departureDateTime", "departureDate", "departure")
        ]
        if wanted and wanted in candidate_times:
            match = flight
            break

    if match is None:
        # The flight we were tracking is not on sale in this response at all:
        # pulled, retimed, or sold out entirely.
        leg.found = False
        leg.multipass = False
        leg.signal = "flight_absent_from_search"
        return leg

    leg.found = True
    leg.flight_number = str(
        match.get("flightNumber") or match.get("carrierCode", "") or ""
    ).strip() or None
    leg.arrives_local_actual = _iso_minute(
        match.get("arrivalDateTime") or match.get("arrival")
    )

    fares = match.get("fares") or match.get("fareBundles") or []
    if isinstance(fares, dict):
        fares = list(fares.values())
    fares = [f for f in fares if isinstance(f, dict)]
    leg.fares = fares
    leg.seats = _seat_count(match, fares)

    multipass_fares = [f for f in fares if _fare_is_multipass(f)]
    if multipass_fares:
        leg.multipass = True
        leg.signal = "multipass_fare_present"
    elif fares:
        leg.multipass = False
        leg.signal = "fares_listed_without_multipass"
    else:
        # Flight is there, but the response told us nothing about fares.
        leg.multipass = None
        leg.signal = "no_fare_detail"

    if leg.seats == 0:
        leg.multipass = False
        leg.signal = "zero_seats"

    return leg


# --- the checker ------------------------------------------------------------


class MultipassChecker:
    def __init__(
        self,
        browser: BrowserSession,
        budget: CheckBudget,
        page_settle_ms: int = 9_000,
    ) -> None:
        self.browser = browser
        self.budget = budget
        self.page_settle_ms = page_settle_ms

    async def check_leg(
        self, origin: str, destination: str, departs_local: str
    ) -> LegAvailability:
        """Ask the logged-in site about one leg."""
        await self.budget.acquire()
        departure_date = departs_local[:10]
        page = await self.browser.new_page()
        collector = ResponseCollector(page, (selectors.API_SEARCH,))
        try:
            await page.goto(
                selectors.flight_search_url(origin, destination, departure_date),
                wait_until="domcontentloaded",
            )
            await self.browser.dismiss_cookie_banner(page)
            try:
                captured = await collector.wait_for(selectors.API_SEARCH, timeout=30.0)
            except asyncio.TimeoutError:
                await page.wait_for_timeout(self.page_settle_ms)
                captured = collector.latest(selectors.API_SEARCH)
                if captured is None:
                    leg = LegAvailability(origin, destination, departs_local)
                    leg.error = "page never issued a flight search"
                    return leg

            if captured.status == 429:
                leg = LegAvailability(origin, destination, departs_local)
                leg.error = "rate limited by the backend"
                return leg

            leg = parse_leg(captured.body, origin, destination, departs_local)

            if leg.multipass is None:
                # The API was unhelpful; fall back to whether the page rendered
                # a Multipass fare control at all.
                try:
                    count = await page.locator(selectors.MULTIPASS_FARE_BUTTON).count()
                    if count:
                        leg.multipass = True
                        leg.signal = "multipass_control_rendered"
                except Exception:
                    pass

            return leg
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            log.warning("leg check %s->%s failed: %s", origin, destination, exc)
            leg = LegAvailability(origin, destination, departs_local)
            leg.error = str(exc)
            return leg
        finally:
            await page.close()

    async def check_itinerary(self, legs: Sequence[dict[str, Any]]) -> ItineraryAvailability:
        """Check every leg. The itinerary is only available if all of them are.

        Legs are checked in order and the walk stops at the first one that is
        gone -- there is no point spending budget on leg 2 of a trip whose
        leg 1 has sold out.
        """
        results: list[LegAvailability] = []
        for leg in legs:
            outcome = await self.check_leg(
                leg["origin"], leg["destination"], leg["departs_local"]
            )
            results.append(outcome)
            if outcome.error:
                return ItineraryAvailability(
                    result=CHECK_FAILED,
                    legs=results,
                    note=f"could not check {outcome.origin}->{outcome.destination}: {outcome.error}",
                )
            if outcome.multipass is False:
                return ItineraryAvailability(
                    result=SOLD_OUT,
                    legs=results,
                    note=f"{outcome.origin}->{outcome.destination}: {outcome.signal}",
                )

        if all(leg.multipass is True for leg in results):
            return ItineraryAvailability(result=AVAILABLE, legs=results)

        return ItineraryAvailability(
            result=UNKNOWN,
            legs=results,
            note="fare detail did not identify a Multipass option; "
                 "stored raw fares for calibration",
        )


def next_check_time(
    now: datetime,
    window_closes: datetime,
    settings: WatcherSettings,
    result: str,
) -> datetime | None:
    """When to look at this candidate again.

    Tighter right after a window opens, because that is when the seat is most
    likely to be taken by someone else.
    """
    if result == AVAILABLE:
        # Something is about to happen with this one; keep it fresh.
        interval = timedelta(minutes=settings.hot_recheck_interval_min)
    elif now + timedelta(minutes=settings.hot_window_min) > window_closes:
        interval = timedelta(minutes=settings.hot_recheck_interval_min)
    else:
        interval = timedelta(minutes=settings.recheck_interval_min)

    nxt = now + interval
    return nxt if nxt < window_closes else None
