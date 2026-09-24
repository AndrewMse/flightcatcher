"""Watcher orchestration: candidate persistence, spend guards, approval handshake."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest

from hopwatch.jobs.sqlite_queue import SqliteJobQueue
from hopwatch.models import BookingWindow, Departure, Itinerary
from hopwatch.network import RouteNetwork
from hopwatch.settings import PassengerSettings, Settings, WatcherSettings
from hopwatch.store import BOOKED, PENDING_APPROVAL, SqliteStore, Store
from hopwatch.timezones import tz_for
from hopwatch.sweep import legs_payload, signature_for
from hopwatch.watcher import Watcher

from .conftest import build_map

UTC = timezone.utc


def dep(net: RouteNetwork, origin: str, dest: str, local: str) -> Departure:
    zone = tz_for(origin, net.airport(origin).country_code)
    aware = datetime.fromisoformat(local).replace(tzinfo=zone)
    return Departure(
        origin=origin,
        destination=dest,
        departs_local=aware,
        departs_utc=aware.astimezone(UTC),
        duration_min=net.duration_min(origin, dest),
        price_amount=None,
        price_currency=None,
    )


def itinerary_for(net: RouteNetwork, now: datetime) -> Itinerary:
    """A two-leg itinerary whose window is open right now."""
    leg1 = dep(net, "OTP", "BUD", "2026-09-20T08:00")
    leg2 = dep(net, "BUD", "EIN", "2026-09-20T14:00")
    itinerary = Itinerary(legs=[leg1, leg2])
    itinerary.window = BookingWindow(
        opens_utc=now - timedelta(hours=1),
        closes_utc=now + timedelta(hours=40),
        first_leg_opens_utc=now - timedelta(hours=9),
        status="open",
    )
    return itinerary


@pytest.fixture
def net() -> RouteNetwork:
    return RouteNetwork(build_map())


@pytest.fixture
def store(tmp_path) -> Store:
    s = SqliteStore(tmp_path / "watcher.db")
    yield s
    s.close()


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = Settings()
    s.database = tmp_path / "watcher.db"
    s.watcher = WatcherSettings(max_open_bookings=1, max_bookings_per_day=2)
    s.passenger = PassengerSettings(first_name="A", last_name="B")
    return s


@pytest.fixture
def watcher(store, settings) -> Watcher:
    return Watcher(store, settings)


def add_want(store: Store, **overrides) -> int:
    fields = {
        "name": "Home", "origin": "OTP", "destination": "EIN",
        "date_from": date.today().isoformat(),
        "date_to": (date.today() + timedelta(days=15)).isoformat(), "max_stops": 1,
        "min_layover_min": 180, "max_layover_min": 1200, "max_detour": 2.2,
        "max_trip_hours": 30.0, "allow_ground_transfer": 0, "after_hour": None,
        "before_hour": None, "auto_request_booking": 0, "active": 1, "notes": "",
    }
    fields.update(overrides)
    return store.add_want(**fields)


# --- identity ---------------------------------------------------------------


def test_signature_is_stable_and_discriminating(net) -> None:
    now = datetime.now(UTC)
    one = itinerary_for(net, now)
    same = itinerary_for(net, now)
    assert signature_for(one) == signature_for(same)

    other = Itinerary(legs=[dep(net, "OTP", "EIN", "2026-09-20T08:00")])
    assert signature_for(one) != signature_for(other)


def test_legs_payload_round_trips_through_the_store(net, store) -> None:
    want_id = add_want(store)
    itinerary = itinerary_for(net, datetime.now(UTC))
    candidate_id, _ = store.upsert_candidate(
        want_id=want_id,
        signature=signature_for(itinerary),
        path=itinerary.path,
        legs=legs_payload(itinerary),
        stops=itinerary.stops,
        total_minutes=itinerary.total_minutes,
        ground_transfer=False,
        staggered_hours=itinerary.window.staggered_hours,
        departs_utc=itinerary.departs_utc,
        window_opens_utc=itinerary.window.opens_utc,
        window_closes_utc=itinerary.window.closes_utc,
    )
    stored = store.get_candidate(candidate_id)
    assert [leg["origin"] for leg in stored.legs] == ["OTP", "BUD"]
    # The booking flow keys off departs_local, so it must survive the trip.
    assert stored.legs[0]["departs_local"].startswith("2026-09-20T08:00")


# --- spend guards -----------------------------------------------------------


def test_open_booking_cap_blocks_further_spending(store, watcher) -> None:
    want_id = add_want(store)
    now = datetime.now(UTC)
    candidate_id, _ = store.upsert_candidate(
        want_id=want_id, signature="s", path=["OTP", "EIN"], legs=[],
        stops=0, total_minutes=168, ground_transfer=False, staggered_hours=0,
        departs_utc=now + timedelta(hours=72),
        window_opens_utc=now, window_closes_utc=now + timedelta(hours=40),
    )

    assert watcher._spending_allowed()
    store.create_booking(candidate_id, "OTP→EIN", {})
    assert not watcher._spending_allowed()  # cap is 1


def test_daily_booking_cap_blocks_further_spending(store, watcher) -> None:
    want_id = add_want(store)
    now = datetime.now(UTC)
    for i in range(2):
        candidate_id, _ = store.upsert_candidate(
            want_id=want_id, signature=f"s{i}", path=["OTP", "EIN"], legs=[],
            stops=0, total_minutes=168, ground_transfer=False, staggered_hours=0,
            departs_utc=now + timedelta(hours=72),
            window_opens_utc=now, window_closes_utc=now + timedelta(hours=40),
        )
        booking_id = store.create_booking(candidate_id, "OTP→EIN", {})
        store.update_booking(booking_id, status=BOOKED, confirmed_at=now)

    assert not watcher._spending_allowed()  # daily cap is 2


# --- the approval handshake -------------------------------------------------


async def test_decide_resolves_a_waiting_approval(store, watcher) -> None:
    want_id = add_want(store)
    now = datetime.now(UTC)
    candidate_id, _ = store.upsert_candidate(
        want_id=want_id, signature="s", path=["OTP", "EIN"], legs=[],
        stops=0, total_minutes=168, ground_transfer=False, staggered_hours=0,
        departs_utc=now + timedelta(hours=72),
        window_opens_utc=now, window_closes_utc=now + timedelta(hours=40),
    )
    booking_id = store.create_booking(candidate_id, "OTP→EIN", {})
    store.update_booking(booking_id, status=PENDING_APPROVAL)

    waiter = asyncio.create_task(
        watcher._await_approval(booking_id, now + timedelta(minutes=5))
    )
    await asyncio.sleep(0.05)

    assert watcher.awaiting_approval() == [booking_id]
    assert watcher.decide(booking_id, True, "discord:me") is True

    approved, who = await waiter
    assert approved is True
    assert who == "discord:me"
    assert store.get_booking(booking_id).decided_by == "discord:me"


async def test_decide_on_nothing_waiting_is_refused(store, watcher) -> None:
    """A tap that lands after the hold lapsed must not resurrect the booking."""
    assert watcher.decide(999, True, "discord:me") is False


async def test_approval_times_out_when_nobody_answers(store, watcher) -> None:
    hold_until = datetime.now(UTC) - timedelta(seconds=1)
    approved, why = await watcher._await_approval(1, hold_until)
    assert approved is False
    assert why == "hold expired"
    assert watcher.awaiting_approval() == []


async def test_a_second_decision_is_refused(store, watcher) -> None:
    now = datetime.now(UTC)
    want_id = add_want(store)
    candidate_id, _ = store.upsert_candidate(
        want_id=want_id, signature="s", path=["OTP", "EIN"], legs=[],
        stops=0, total_minutes=168, ground_transfer=False, staggered_hours=0,
        departs_utc=now + timedelta(hours=72),
        window_opens_utc=now, window_closes_utc=now + timedelta(hours=40),
    )
    booking_id = store.create_booking(candidate_id, "OTP→EIN", {})

    waiter = asyncio.create_task(
        watcher._await_approval(booking_id, now + timedelta(minutes=5))
    )
    await asyncio.sleep(0.05)

    assert watcher.decide(booking_id, True, "web") is True
    assert watcher.decide(booking_id, False, "discord:me") is False
    await waiter


def test_status_snapshot(watcher) -> None:
    status = watcher.status()
    assert status["passenger_configured"] is True
    assert status["open_bookings"] == 0
    assert "checks_remaining_this_hour" in status


# --- scheduling -------------------------------------------------------------


def test_schedule_once_queues_one_sweep_per_want_per_slot(store, settings, tmp_path) -> None:
    queue = SqliteJobQueue(tmp_path / "watcher.db")
    watcher = Watcher(store, settings, queue=queue)
    add_want(store, name="a")
    add_want(store, name="b", active=0)
    now = datetime.now(UTC)

    assert watcher.schedule_once(now) == 1
    assert watcher.last_search_at == now
    assert watcher.schedule_once(now + timedelta(seconds=30)) == 0
    assert queue.depth().visible == 1
    queue.close()


def test_schedule_once_expires_closed_candidates(store, settings, tmp_path) -> None:
    queue = SqliteJobQueue(tmp_path / "watcher.db")
    watcher = Watcher(store, settings, queue=queue)
    want_id = add_want(store)
    now = datetime.now(UTC)
    store.upsert_candidate(
        want_id=want_id, signature="old", path=["OTP", "EIN"], legs=[], stops=0,
        total_minutes=168, ground_transfer=False, staggered_hours=0,
        departs_utc=now - timedelta(hours=1), window_opens_utc=now - timedelta(hours=73),
        window_closes_utc=now - timedelta(hours=4),
    )
    watcher.schedule_once(now)
    assert store.list_candidates(status="watching") == []
    queue.close()


# --- restart safety and health ----------------------------------------------


def test_status_safe_to_restart_when_idle(watcher) -> None:
    assert watcher.status()["safe_to_restart"] is True


@pytest.mark.parametrize("status", ["preparing", "pending_approval", "approved", "booking"])
def test_status_not_safe_to_restart_with_an_open_booking(store, watcher, status) -> None:
    want_id = add_want(store)
    now = datetime.now(UTC)
    candidate_id, _ = store.upsert_candidate(
        want_id=want_id, signature="s", path=["OTP", "EIN"], legs=[],
        stops=0, total_minutes=168, ground_transfer=False, staggered_hours=0,
        departs_utc=now + timedelta(hours=72),
        window_opens_utc=now, window_closes_utc=now + timedelta(hours=40),
    )
    booking_id = store.create_booking(candidate_id, "OTP→EIN", {})
    store.update_booking(booking_id, status=status)
    assert watcher.status()["safe_to_restart"] is False


def test_status_reports_pipeline_depth(store, settings, tmp_path) -> None:
    queue = SqliteJobQueue(tmp_path / "watcher.db")
    queue.send({"job": "search:1:1"})
    status = Watcher(store, settings, queue=queue).status()
    assert status["pipeline"] == {"waiting": 1, "in_progress": 0, "dead": 0}
    queue.close()


async def test_health_pass_alerts_once_and_beats(store, settings, tmp_path) -> None:
    class Recorder:
        def __init__(self) -> None:
            self.problems: list[str] = []

        async def problem(self, title: str, detail: str) -> None:
            self.problems.append(title)

    queue = SqliteJobQueue(tmp_path / "watcher.db", visibility_s=0, max_receives=1)
    queue.send({"job": "poison"})
    queue.receive()
    queue.receive()  # second delivery over the limit: dead-lettered
    notifier = Recorder()
    watcher = Watcher(store, settings, notifier=notifier, queue=queue)

    await watcher.health_pass(datetime.now(UTC))
    await watcher.health_pass(datetime.now(UTC))
    assert notifier.problems == ["Pipeline: dead_letters"]
    assert "booker" in store.heartbeats()
    queue.close()
