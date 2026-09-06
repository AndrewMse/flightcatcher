"""Sweeping one want: discovery, persistence, and honest failure reporting."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from hopwatch.models import BookingWindow, Departure, Itinerary
from hopwatch.network import RouteNetwork
from hopwatch.search import SearchResult
from hopwatch.store import SqliteStore
from hopwatch.sweep import PermanentJobError, SweepResult, sweep_want
from hopwatch.timezones import tz_for

from .conftest import build_map

UTC = timezone.utc


def dep(net: RouteNetwork, origin: str, dest: str, local: str) -> Departure:
    zone = tz_for(origin, net.airport(origin).country_code)
    aware = datetime.fromisoformat(local).replace(tzinfo=zone)
    return Departure(
        origin=origin, destination=dest, departs_local=aware,
        departs_utc=aware.astimezone(UTC), duration_min=net.duration_min(origin, dest),
        price_amount=None, price_currency=None,
    )


def open_itinerary(net: RouteNetwork) -> Itinerary:
    now = datetime.now(UTC)
    itinerary = Itinerary(
        legs=[dep(net, "OTP", "BUD", "2026-09-20T08:00"), dep(net, "BUD", "EIN", "2026-09-20T14:00")]
    )
    itinerary.window = BookingWindow(
        opens_utc=now - timedelta(hours=1), closes_utc=now + timedelta(hours=40),
        first_leg_opens_utc=now - timedelta(hours=9), status="open",
    )
    return itinerary


@pytest.fixture
def net() -> RouteNetwork:
    return RouteNetwork(build_map())


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "sweep.db")
    yield s
    s.close()


def add_want(store, **overrides) -> int:
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


def test_sweep_persists_candidates_once(net, store, monkeypatch) -> None:
    want_id = add_want(store)
    found = open_itinerary(net)
    monkeypatch.setattr(
        "hopwatch.sweep.search",
        lambda *a, **k: SearchResult(itineraries=[found], paths_considered=1, routes_queried=2),
    )

    result = sweep_want(store, None, net, store.get_want(want_id))
    assert result == SweepResult(
        itineraries=1, new_candidates=1, complete=True, failed_routes=[],
        paths_considered=1, routes_queried=2,
    )
    assert [c.path for c in store.list_candidates()] == [["OTP", "BUD", "EIN"]]
    assert store.get_want(want_id).last_searched_at is not None

    # Seen again on the next sweep: refreshed, not duplicated.
    assert sweep_want(store, None, net, store.get_want(want_id)).new_candidates == 0
    assert len(store.list_candidates()) == 1


def test_closed_itineraries_are_not_persisted(net, store, monkeypatch) -> None:
    add_want(store)
    stale = open_itinerary(net)
    stale.window.status = "closed"
    monkeypatch.setattr("hopwatch.sweep.search", lambda *a, **k: SearchResult(itineraries=[stale]))
    assert sweep_want(store, None, net, store.list_wants()[0]).new_candidates == 0
    assert store.list_candidates() == []


def test_incomplete_sweep_is_reported_loudly(net, store, monkeypatch) -> None:
    add_want(store)
    monkeypatch.setattr(
        "hopwatch.sweep.search",
        lambda *a, **k: SearchResult(itineraries=[], failed_routes=[("OTP", "WAW")]),
    )
    result = sweep_want(store, None, net, store.list_wants()[0])
    assert result.complete is False
    assert result.failed_routes == ["OTP→WAW"]
    events = store.list_events()
    assert any("incomplete" in e["message"] and e["level"] == "warning" for e in events)


def test_unresolvable_want_fails_permanently_and_says_so(net, store) -> None:
    add_want(store, origin="Narnia")
    with pytest.raises(PermanentJobError):
        sweep_want(store, None, net, store.list_wants()[0])
    assert any("could not resolve" in e["message"] for e in store.list_events())


def test_want_whose_dates_have_passed_is_a_no_op(net, store, monkeypatch) -> None:
    add_want(store, date_from="2020-01-01", date_to="2020-01-05")
    monkeypatch.setattr("hopwatch.sweep.search", lambda *a, **k: pytest.fail("searched"))
    result = sweep_want(store, None, net, store.list_wants()[0])
    assert result.itineraries == 0 and result.complete is True
