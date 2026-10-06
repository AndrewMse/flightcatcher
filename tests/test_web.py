"""Web API tests.

The network is stubbed out: place resolution uses the synthetic route map from
conftest rather than reaching Wizz Air.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from hopwatch.jobs.sqlite_queue import SqliteJobQueue
from hopwatch.network import RouteNetwork
from hopwatch.settings import Settings
from hopwatch.store import PENDING_APPROVAL, SqliteStore, Store
from hopwatch.web.app import create_app

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
    store = SqliteStore(tmp_path / "web.db")
    queue = SqliteJobQueue(tmp_path / "web.db")
    settings = Settings()
    watcher = FakeWatcher(store)
    app = create_app(store, settings, watcher, queue=queue)
    app.state.network = RouteNetwork(build_map())
    with TestClient(app) as test_client:
        test_client.store = store
        test_client.queue = queue
        test_client.watcher = watcher
        yield test_client
    queue.close()
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
    assert "Hopwatch" in page.text


# --- queued sweeps ----------------------------------------------------------


def test_search_now_enqueues_a_job(client) -> None:
    want_id = client.post("/api/wants", json=WANT).json()["id"]
    response = client.post(f"/api/wants/{want_id}/search")
    assert response.status_code == 202
    body = response.json()
    assert body["job"].startswith(f"manual:{want_id}:")
    assert body["created"] is True
    assert client.queue.depth().visible == 1

    job = client.get(f"/api/jobs/{body['job']}").json()
    assert job["status"] == "queued"
    assert job["want_id"] == want_id


def test_search_now_twice_is_one_job(client, monkeypatch) -> None:
    # Pin the clock: two clicks either side of a minute boundary are rightly
    # two jobs, and the wall clock occasionally puts them there.
    import hopwatch.web.app as web_app

    real = web_app.enqueue_manual
    fixed = datetime(2026, 9, 9, 12, 0, 5, tzinfo=UTC)
    monkeypatch.setattr(web_app, "enqueue_manual", lambda store, queue, want_id, now: real(store, queue, want_id, fixed))
    want_id = client.post("/api/wants", json=WANT).json()["id"]
    first = client.post(f"/api/wants/{want_id}/search").json()
    second = client.post(f"/api/wants/{want_id}/search").json()
    assert first["job"] == second["job"]
    assert second["created"] is False
    assert client.queue.depth().visible == 1


def test_search_now_on_a_paused_want_is_refused(client) -> None:
    want_id = client.post("/api/wants", json=WANT | {"active": False}).json()["id"]
    assert client.post(f"/api/wants/{want_id}/search").status_code == 409


def test_search_now_on_a_missing_want_is_404(client) -> None:
    assert client.post("/api/wants/999/search").status_code == 404


def test_search_now_without_a_queue_is_503(tmp_path) -> None:
    store = SqliteStore(tmp_path / "noq.db")
    app = create_app(store, Settings(), FakeWatcher(store))
    want_id = store.add_want(
        name="x", origin="OTP", destination="EIN", date_from="2026-09-15",
        date_to="2026-09-30", max_stops=1, min_layover_min=180, max_layover_min=1200,
        max_detour=2.2, max_trip_hours=30.0, allow_ground_transfer=0, after_hour=None,
        before_hour=None, auto_request_booking=0, active=1, notes="",
    )
    with TestClient(app) as test_client:
        assert test_client.post(f"/api/wants/{want_id}/search").status_code == 503
    store.close()


def test_jobs_listing_and_lookup(client) -> None:
    client.store.create_job("search:1:1", "search_want", 1)
    client.store.create_job("search:1:2", "search_want", 1)
    client.store.finish_job("search:1:2", "done")
    queued = client.get("/api/jobs", params={"status": "queued"}).json()
    assert [j["key"] for j in queued] == ["search:1:1"]
    assert len(client.get("/api/jobs").json()) == 2
    assert client.get("/api/jobs/search:9:9").status_code == 404


def test_search_runs_endpoint(client) -> None:
    run_id = client.store.start_search_run(want_id=5, job_key="search:5:1")
    client.store.finish_search_run(run_id, 5, "ok", upstream_calls=12, cache_hits=30)
    runs = client.get("/api/search-runs", params={"want_id": 5}).json()
    assert [(r["id"], r["upstream_calls"], r["cache_hits"]) for r in runs] == [(run_id, 12, 30)]
    assert client.get("/api/search-runs", params={"want_id": 6}).json() == []


def test_health_endpoint(client) -> None:
    signals = client.get("/api/health").json()["signals"]
    assert {s["name"] for s in signals} == {"dead_letters", "queue_stalled", "job_failures"}
    assert not any(s["firing"] for s in signals)
