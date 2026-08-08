"""Layer 4: the watcher.

Three loops on different clocks, because the work has very different costs:

* **search** — cheap, unauthenticated layer-2 sweeps of every active want, to
  discover itineraries and their 72h unlock times. Every 15 minutes.
* **check** — expensive, authenticated Multipass availability checks, spent
  only on candidates whose window is actually open, under a hard budget.
* **booking** — prepares approved-in-principle bookings up to the confirm
  button, waits for a human, and expires stale holds.

The reason for the split is the staggered-window problem: a candidate becomes
interesting at `max(departure − 72h)` across its legs, and that moment can be
04:00. The cheap loop knows *when* to care; the expensive loop only runs then.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import config
from .booking import BookingAborted, BookingFlow, PaymentWallReached, PreparedBooking
from .browser import BrowserSession, BrowserUnavailable, NotLoggedIn
from .client import WizzClient
from .models import Itinerary
from .multipass import CheckBudget, MultipassChecker, next_check_time
from .network import RouteNetwork
from .notify import Notifier, NullNotifier, describe
from .search import SearchOptions, search
from .settings import Settings
from .store import (
    APPROVED,
    AVAILABLE,
    BOOKED,
    BOOKING,
    CHECK_FAILED,
    FAILED,
    HOLD_EXPIRED,
    PENDING_APPROVAL,
    PREPARING,
    REJECTED,
    Candidate,
    Store,
    Want,
)

log = logging.getLogger(__name__)
UTC = timezone.utc


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


class Watcher:
    def __init__(
        self,
        store: Store,
        settings: Settings,
        notifier: Notifier | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.notifier = notifier or NullNotifier()
        self.budget = CheckBudget(settings.watcher)

        self._client = WizzClient()
        self._network: RouteNetwork | None = None
        self._browser: BrowserSession | None = None
        self._browser_lock = asyncio.Lock()
        self._approvals: dict[int, asyncio.Future] = {}
        self._running = False
        self._tasks: set[asyncio.Task] = set()

        self.last_search_at: datetime | None = None
        self.last_check_at: datetime | None = None
        self.session_ok: bool | None = None
        self.last_problem: str | None = None

    # --- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        self._running = True
        self.store.log("watcher", "Watcher started")
        await asyncio.gather(
            self._search_loop(),
            self._check_loop(),
            self._maintenance_loop(),
            self._keepalive_loop(),
        )

    async def stop(self) -> None:
        self._running = False
        for task in list(self._tasks):
            task.cancel()
        if self._browser:
            # Snapshot before closing: if the close is interrupted, Chromium
            # never flushes its cookie store and the session is lost.
            try:
                await self._browser.save_cookies()
            except Exception:  # noqa: BLE001
                log.debug("could not snapshot cookies during shutdown")
            await self._browser.close()
            self._browser = None
        self._client.close()
        self.store.log("watcher", "Watcher stopped")

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # --- shared resources ---------------------------------------------------

    async def network(self) -> RouteNetwork:
        if self._network is None:
            raw = await asyncio.to_thread(self._client.route_map)
            self._network = RouteNetwork(raw)
        return self._network

    async def browser(self) -> BrowserSession:
        """Lazily start the logged-in browser, reusing it across checks."""
        async with self._browser_lock:
            if self._browser is None:
                session = BrowserSession(self.settings.browser)
                await session.start()
                self._browser = session

                self.session_ok = await session.is_logged_in()
                if not self.session_ok:
                    # The profile may simply have lost its cookies to an
                    # unclean shutdown rather than the session expiring, so
                    # try the backup before asking for a fresh login.
                    if await session.restore_cookies():
                        self.session_ok = await session.is_logged_in()
                        if self.session_ok:
                            self.store.log(
                                "session",
                                "Recovered the Wizz session from the cookie backup",
                                level="warning",
                            )

                if not self.session_ok:
                    raise NotLoggedIn(
                        "Wizz Air session is not usable. Run 'flightcatcher login'."
                    )
                await session.save_cookies()
            return self._browser

    async def _drop_browser(self) -> None:
        async with self._browser_lock:
            if self._browser:
                try:
                    await self._browser.close()
                except Exception:
                    pass
                self._browser = None

    # --- loop 1: cheap discovery -------------------------------------------

    async def _search_loop(self) -> None:
        while self._running:
            try:
                await self.search_all_wants()
            except Exception as exc:  # noqa: BLE001
                log.exception("search pass failed")
                self.last_problem = str(exc)
                self.store.log("search", f"Search pass failed: {exc}", level="error")
            await asyncio.sleep(self.settings.watcher.search_interval_min * 60)

    async def search_all_wants(self) -> int:
        wants = self.store.list_wants(active_only=True)
        if not wants:
            return 0
        network = await self.network()
        total_new = 0
        for want in wants:
            total_new += await self._search_want(want, network)
        self.last_search_at = datetime.now(UTC)
        self.store.expire_stale_candidates(self.last_search_at)
        return total_new

    async def _search_want(self, want: Want, network: RouteNetwork) -> int:
        origins = network.resolve(want.origin)
        destinations = network.resolve(want.destination)
        if not origins or not destinations:
            self.store.log(
                "search",
                f"Want {want.name!r}: could not resolve "
                f"{want.origin!r} or {want.destination!r}",
                level="error",
                want_id=want.id,
            )
            return 0

        today = date.today()
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
            return 0

        result = await asyncio.to_thread(
            search, self._client, network, origins, destinations, opts
        )

        if result.failed_routes:
            legs = ", ".join(f"{a}→{b}" for a, b in result.failed_routes)
            self.store.log(
                "search",
                f"Want {want.name!r}: incomplete, could not check {legs}",
                level="warning",
                want_id=want.id,
            )

        new = 0
        for itinerary in result.itineraries:
            window = itinerary.window
            if window is None or window.status == "closed":
                continue
            _, was_new = self.store.upsert_candidate(
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

        self.store.mark_want_searched(want.id)
        if new:
            self.store.log(
                "search",
                f"Want {want.name!r}: {new} new candidate(s), "
                f"{len(result.itineraries)} total",
                want_id=want.id,
            )
        return new

    # --- loop 2: authenticated checks ---------------------------------------

    async def _check_loop(self) -> None:
        while self._running:
            try:
                did = await self.check_due_candidates()
                await asyncio.sleep(30 if did else 60)
            except (NotLoggedIn, BrowserUnavailable) as exc:
                self.session_ok = False
                self.last_problem = str(exc)
                self.store.log("session", str(exc), level="error")
                await self.notifier.problem("Wizz Air session unusable", str(exc))
                await self._drop_browser()
                await asyncio.sleep(600)
            except Exception as exc:  # noqa: BLE001
                log.exception("check pass failed")
                self.last_problem = str(exc)
                self.store.log("check", f"Check pass failed: {exc}", level="error")
                await self._drop_browser()
                await asyncio.sleep(120)

    async def check_due_candidates(self) -> int:
        now = datetime.now(UTC)
        if self.budget.remaining <= 0:
            return 0

        due = self.store.candidates_due_for_check(now, limit=5)
        if not due:
            return 0

        browser = await self.browser()
        checker = MultipassChecker(browser, self.budget)
        checked = 0

        for candidate in due:
            outcome = await checker.check_itinerary(candidate.legs)
            nxt = next_check_time(
                datetime.now(UTC),
                candidate.window_closes_utc,
                self.settings.watcher,
                outcome.result,
            )
            self.store.record_check(candidate.id, outcome.result, outcome.to_dict(), nxt)
            checked += 1
            self.last_check_at = datetime.now(UTC)

            if outcome.result == CHECK_FAILED:
                self.store.log(
                    "check",
                    f"Could not check {describe(candidate)}: {outcome.note}",
                    level="warning",
                    candidate_id=candidate.id,
                )
                continue

            if outcome.result == AVAILABLE:
                await self._on_available(candidate)

        return checked

    async def _on_available(self, candidate: Candidate) -> None:
        fresh = self.store.get_candidate(candidate.id)
        if fresh is None:
            return

        if fresh.alerted_at is None:
            self.store.mark_alerted(fresh.id)
            await self.notifier.candidate_available(fresh)

        want = self.store.get_want(fresh.want_id)
        if not want or not want.auto_request_booking:
            return
        if self.store.has_open_booking(fresh.id):
            return
        if not self._spending_allowed():
            return
        if not self.settings.passenger.is_complete:
            self.store.log(
                "booking",
                "Skipping booking prep: passenger details are not configured",
                level="warning",
                candidate_id=fresh.id,
            )
            return

        self._spawn(self._run_booking(fresh))

    def _spending_allowed(self) -> bool:
        watcher = self.settings.watcher
        if self.store.count_open_bookings() >= watcher.max_open_bookings:
            self.store.log(
                "booking",
                f"Holding off: {watcher.max_open_bookings} bookings already in flight",
                level="warning",
            )
            return False
        since = datetime.now(UTC) - timedelta(days=1)
        if self.store.count_bookings_since(since) >= watcher.max_bookings_per_day:
            self.store.log(
                "booking",
                f"Holding off: daily cap of {watcher.max_bookings_per_day} bookings reached",
                level="warning",
            )
            return False
        return True

    # --- booking with human approval ----------------------------------------

    async def _run_booking(self, candidate: Candidate) -> None:
        booking_id = self.store.create_booking(
            candidate.id,
            summary=describe(candidate),
            detail={"legs": candidate.legs},
        )
        prepared: PreparedBooking | None = None
        try:
            browser = await self.browser()
            flow = BookingFlow(browser, self.settings.browser, self.settings.passenger)
            prepared = await flow.prepare(candidate.legs)

            hold_until = datetime.now(UTC) + timedelta(
                minutes=self.settings.watcher.approval_hold_min
            )
            detail = prepared.to_detail()
            detail["trip_credits"] = len(candidate.legs)
            detail["cost_eur"] = config.MULTIPASS_FARE_EUR * len(candidate.legs)
            self.store.update_booking(
                booking_id,
                status=PENDING_APPROVAL,
                summary=prepared.summary,
                detail=detail,
                screenshot_path=str(prepared.legs[0].screenshot)
                if prepared.legs and prepared.legs[0].screenshot
                else None,
                hold_expires_at=hold_until,
            )

            booking = self.store.get_booking(booking_id)
            await self.notifier.approval_needed(booking, candidate)

            approved, who = await self._await_approval(booking_id, hold_until)

            if not approved:
                await flow.abandon(prepared)
                current = self.store.get_booking(booking_id)
                if current and current.status == PENDING_APPROVAL:
                    self.store.update_booking(
                        booking_id, status=HOLD_EXPIRED, decided_at=datetime.now(UTC)
                    )
                self.store.log(
                    "booking",
                    f"Booking {booking_id} not confirmed ({who})",
                    level="warning",
                    booking_id=booking_id,
                )
                await self.notifier.booking_finished(
                    self.store.get_booking(booking_id), candidate
                )
                return

            self.store.update_booking(
                booking_id, status=BOOKING, decided_at=datetime.now(UTC), decided_by=who
            )
            outcome = await flow.confirm(prepared)

            if outcome["ok"]:
                references = ", ".join(
                    c.get("reference") or "?" for c in outcome["confirmed"]
                )
                self.store.update_booking(
                    booking_id,
                    status=BOOKED,
                    confirmed_at=datetime.now(UTC),
                    confirmation=references,
                    detail={**detail, "result": outcome},
                )
                self.store.log(
                    "booking",
                    f"Booked {describe(candidate)} ({references})",
                    level="success",
                    booking_id=booking_id,
                )
            else:
                level = "error"
                message = f"Booking {booking_id} failed: {outcome.get('error')}"
                if outcome.get("partial"):
                    message = (
                        f"PARTIAL BOOKING — {len(outcome['confirmed'])} leg(s) confirmed, "
                        f"then {outcome['failed_leg']} failed. You may be holding a "
                        f"ticket to a connection with no onward flight. Act now."
                    )
                self.store.update_booking(
                    booking_id,
                    status=FAILED,
                    error=message,
                    detail={**detail, "result": outcome},
                )
                self.store.log("booking", message, level=level, booking_id=booking_id)
                await self.notifier.problem("Booking failed", message)

            await self.notifier.booking_finished(
                self.store.get_booking(booking_id), candidate
            )

        except PaymentWallReached as exc:
            message = (
                f"Stopped before a payment form for {describe(candidate)}. "
                f"This bot never enters card details — finish it yourself if you want it. "
                f"({exc})"
            )
            self.store.update_booking(booking_id, status=FAILED, error=message)
            self.store.log("booking", message, level="error", booking_id=booking_id)
            await self.notifier.problem("Payment form reached — human needed", message)
        except (BookingAborted, NotLoggedIn, BrowserUnavailable) as exc:
            self.store.update_booking(booking_id, status=FAILED, error=str(exc))
            self.store.log(
                "booking", f"Booking {booking_id} aborted: {exc}", level="error",
                booking_id=booking_id,
            )
            await self.notifier.problem("Booking aborted", str(exc))
        except Exception as exc:  # noqa: BLE001
            log.exception("booking %s blew up", booking_id)
            self.store.update_booking(booking_id, status=FAILED, error=str(exc))
            await self.notifier.problem("Booking error", str(exc))
        finally:
            self._approvals.pop(booking_id, None)

    async def _await_approval(
        self, booking_id: int, hold_until: datetime
    ) -> tuple[bool, str]:
        """Block until a human decides, or the held seat goes stale."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._approvals[booking_id] = future
        timeout = max(5.0, (hold_until - datetime.now(UTC)).total_seconds())
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            return False, "hold expired"
        finally:
            self._approvals.pop(booking_id, None)

    def decide(self, booking_id: int, approved: bool, by: str) -> bool:
        """Record a human decision. Called by Discord and the web UI.

        Returns False if nothing was waiting -- an approval that arrives after
        the hold expired must not resurrect a dead booking.
        """
        future = self._approvals.get(booking_id)
        if future is None or future.done():
            return False
        self.store.update_booking(
            booking_id,
            status=APPROVED if approved else REJECTED,
            decided_at=datetime.now(UTC),
            decided_by=by,
        )
        future.set_result((approved, by))
        return True

    def awaiting_approval(self) -> list[int]:
        return [bid for bid, fut in self._approvals.items() if not fut.done()]

    # --- loop 4: keep the session warm --------------------------------------

    async def _keepalive_loop(self) -> None:
        """Touch the session periodically and snapshot its cookies.

        Two separate problems, one loop. Wizz expires idle sessions, and the
        watcher can go hours between authenticated checks -- so without this
        the session is reliably dead at the one moment it matters. And cookies
        only reach disk on a clean browser close, so the snapshot means an
        unclean exit costs nothing.

        The first failure is announced loudly; repeats are not, because a
        Discord alert every twenty minutes is how people learn to ignore
        Discord alerts.
        """
        interval = self.settings.browser.keepalive_min
        if interval <= 0:
            return

        warned = False
        while self._running:
            await asyncio.sleep(interval * 60)
            if not self._running:
                return
            try:
                browser = await self.browser()
                alive = await browser.touch()
                self.session_ok = alive

                if alive:
                    if warned:
                        self.store.log(
                            "session", "Wizz session is healthy again", level="success"
                        )
                        warned = False
                    continue

                if not warned:
                    warned = True
                    message = (
                        "Wizz Air session has expired. Run 'flightcatcher login' "
                        "to sign in again — availability checks and booking are "
                        "paused until you do."
                    )
                    self.store.log("session", message, level="error")
                    await self.notifier.problem("Wizz session expired", message)
            except (NotLoggedIn, BrowserUnavailable) as exc:
                self.session_ok = False
                if not warned:
                    warned = True
                    self.store.log("session", str(exc), level="error")
                    await self.notifier.problem("Wizz session unusable", str(exc))
            except Exception:  # noqa: BLE001
                log.exception("keepalive pass failed")

    # --- loop 3: housekeeping ----------------------------------------------

    async def _maintenance_loop(self) -> None:
        while self._running:
            try:
                now = datetime.now(UTC)
                self.store.expire_stale_candidates(now)
                for stale in self.store.expire_held_bookings(now):
                    self.store.log(
                        "booking",
                        f"Booking {stale.id} expired without approval",
                        level="warning",
                        booking_id=stale.id,
                    )
                self.store.prune_events(keep_days=30)
            except Exception:  # noqa: BLE001
                log.exception("maintenance pass failed")
            await asyncio.sleep(300)

    # --- status for the UI --------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "session_ok": self.session_ok,
            "last_search_at": self.last_search_at.isoformat()
            if self.last_search_at
            else None,
            "last_check_at": self.last_check_at.isoformat()
            if self.last_check_at
            else None,
            "checks_remaining_this_hour": self.budget.remaining,
            "awaiting_approval": self.awaiting_approval(),
            "open_bookings": self.store.count_open_bookings(),
            "last_problem": self.last_problem,
            "passenger_configured": self.settings.passenger.is_complete,
        }
