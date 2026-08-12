"""Web API tests.

The network is stubbed out: place resolution uses the synthetic route map from
conftest rather than reaching Wizz Air.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from flightcatcher.network import RouteNetwork
from flightcatcher.settings import Settings
from flightcatcher.store import PENDING_APPROVAL, Store
from flightcatcher.web.app import create_app

from .conftest import build_map

UTC = timezone.utc


class FakeWatcher:
    """Stands in for the real watcher: records decisions, refuses stale ones."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.decisions: list[tuple[int, bool, str]] = []
        self.waiting: set[int] = set()

    def status(self) -> dict:
        return {"running": True, "session_ok": True, "open_bookings": 0}

    def decide(self, booking_id: int, approved: bool, by: str) -> bool:
        if booking_id not in self.waiting:
            return False
        self.waiting.discard(booking_id)
        self.decisions.append((booking_id, approved, by))
        return True


@pytest.fixture
def client(tmp_path):
    store = Store(tmp_path / "web.db")
    settings = Settings()
    watcher = FakeWatcher(store)
    app = create_app(store, settings, watcher)
    app.state.network = RouteNetwork(build_map())
    with TestClient(app) as test_client:
        test_client.store = store
        test_client.watcher = watcher
        yield test_client
    store.close()


WANT = {
    "name": "Home",
    "origin": "Bucharest",
    "destination": "EIN",
    "date_from": "2026-09-15",
    "date_to": "2026-09-30",
}


# --- wants ------------------------------------------------------------------


def test_create_and_list_a_want(client) -> None:
    created = client.post("/api/wants", json=WANT)
    assert created.status_code == 201
    assert created.json()["origin"] == "Bucharest"

    listed = client.get("/api/wants").json()
    assert len(listed) == 1
    assert listed[0]["name"] == "Home"


def test_unknown_places_are_rejected_at_creation(client) -> None:
    bad = client.post("/api/wants", json={**WANT, "destination": "Narnia"})
    assert bad.status_code == 400
    assert "Narnia" in bad.json()["detail"]
    assert client.get("/api/wants").json() == []


def test_backwards_dates_are_rejected(client) -> None:
    bad = client.post(
        "/api/wants", json={**WANT, "date_from": "2026-09-30", "date_to": "2026-09-15"}
    )
    assert bad.status_code == 400


def test_pausing_and_deleting_a_want(client) -> None:
    want_id = client.post("/api/wants", json=WANT).json()["id"]

    paused = client.patch(f"/api/wants/{want_id}", json={"active": False})
    assert paused.json()["active"] is False

    assert client.delete(f"/api/wants/{want_id}").status_code == 204
    assert client.get("/api/wants").json() == []
    assert client.patch(f"/api/wants/{want_id}", json={"active": True}).status_code == 404


# --- candidates -------------------------------------------------------------


def seed_candidate(store: Store, want_id: int, opens_in_hours: float = -1.0):
    now = datetime.now(UTC)
    opens = now + timedelta(hours=opens_in_hours)
    return store.upsert_candidate(
        want_id=want_id,
        signature="sig",
        path=["OTP", "BUD", "EIN"],
        legs=[
            {"origin": "OTP", "destination": "BUD",
             "departs_local": "2026-09-20T22:55:00+03:00"},
            {"origin": "BUD", "destination": "EIN",
             "departs_local": "2026-09-21T06:15:00+02:00"},
        ],
        stops=1,
        total_minutes=604,
        ground_transfer=False,
        staggered_hours=8.3,
        departs_utc=opens + timedelta(hours=72),
        window_opens_utc=opens,
        window_closes_utc=now + timedelta(hours=40),
    )


def test_candidates_are_served_with_derived_fields(client) -> None:
    want_id = client.post("/api/wants", json=WANT).json()["id"]
    seed_candidate(client.store, want_id)

    candidates = client.get("/api/candidates").json()
    assert len(candidates) == 1
    assert candidates[0]["window_status"] == "open"
    assert candidates[0]["trip_credits"] == 2
    assert candidates[0]["staggered_hours"] == 8.3
    assert candidates[0]["want_name"] == "Home"


# --- approvals: the part that spends money ----------------------------------


def seed_pending_booking(client, want_id: int) -> int:
    candidate_id, _ = seed_candidate(client.store, want_id)
    booking_id = client.store.create_booking(candidate_id, "OTP→BUD→EIN", {"cost_eur": 20})
    client.store.update_booking(
        booking_id,
        status=PENDING_APPROVAL,
        hold_expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    return booking_id


def test_pending_bookings_carry_their_candidate(client) -> None:
    want_id = client.post("/api/wants", json=WANT).json()["id"]
    booking_id = seed_pending_booking(client, want_id)

    pending = client.get("/api/bookings/pending").json()
    assert len(pending) == 1
    assert pending[0]["id"] == booking_id
    assert pending[0]["candidate"]["path"] == ["OTP", "BUD", "EIN"]


def test_approving_reaches_the_watcher(client) -> None:
    want_id = client.post("/api/wants", json=WANT).json()["id"]
    booking_id = seed_pending_booking(client, want_id)
    client.watcher.waiting.add(booking_id)

    res = client.post(f"/api/bookings/{booking_id}/decision", json={"approved": True})
    assert res.status_code == 200
    assert client.watcher.decisions == [(booking_id, True, "web")]


def test_a_decision_on_a_lapsed_hold_is_refused_not_queued(client) -> None:
    """Approving after the hold expired must never book anything later."""
    want_id = client.post("/api/wants", json=WANT).json()["id"]
    booking_id = seed_pending_booking(client, want_id)
    # Watcher is not waiting on it -- the hold has lapsed.

    res = client.post(f"/api/bookings/{booking_id}/decision", json={"approved": True})
    assert res.status_code == 409
    assert client.watcher.decisions == []


def test_deciding_a_booking_that_does_not_exist(client) -> None:
    assert client.post("/api/bookings/999/decision", json={"approved": True}).status_code == 404


def test_missing_screenshot_is_a_404_not_a_crash(client) -> None:
    want_id = client.post("/api/wants", json=WANT).json()["id"]
    booking_id = seed_pending_booking(client, want_id)
    assert client.get(f"/api/bookings/{booking_id}/screenshot").status_code == 404


# --- misc -------------------------------------------------------------------


def test_status_reports_config_problems(client) -> None:
    data = client.get("/api/status").json()
    assert data["watcher"]["running"] is True
    # No passenger configured in a default Settings().
    assert any("passenger" in p for p in data["config_problems"])


def test_place_lookup(client) -> None:
    assert [p["iata"] for p in client.get("/api/places?q=Bucharest").json()] == ["BBU", "OTP"]
    assert client.get("/api/places?q=Narnia").json() == []


def test_events_feed(client) -> None:
    client.store.log("test", "hello")
    events = client.get("/api/events").json()
    assert events[0]["message"] == "hello"


def test_frontend_is_served(client) -> None:
    page = client.get("/")
    assert page.status_code == 200
    assert "FlightCatcher" in page.text
