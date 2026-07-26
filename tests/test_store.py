from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from flightcatcher.store import (
    AVAILABLE,
    BOOKED,
    EXPIRED,
    HOLD_EXPIRED,
    PENDING_APPROVAL,
    SOLD_OUT,
    WATCHING,
    Store,
)

UTC = timezone.utc


@pytest.fixture
def store(tmp_path) -> Store:
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def make_want(store: Store, **overrides) -> int:
    fields = {
        "name": "Home",
        "origin": "Bucharest",
        "destination": "EIN",
        "date_from": "2026-09-15",
        "date_to": "2026-09-30",
        "max_stops": 1,
        "min_layover_min": 180,
        "max_layover_min": 1200,
        "max_detour": 2.2,
        "max_trip_hours": 30.0,
        "allow_ground_transfer": 0,
        "after_hour": None,
        "before_hour": None,
        "auto_request_booking": 0,
        "active": 1,
        "notes": "",
    }
    fields.update(overrides)
    return store.add_want(**fields)


def make_candidate(store: Store, want_id: int, *, opens, closes, signature="sig1"):
    return store.upsert_candidate(
        want_id=want_id,
        signature=signature,
        path=["OTP", "BUD", "EIN"],
        legs=[
            {"origin": "OTP", "destination": "BUD", "departs_local": "2026-09-20T22:55:00+03:00"},
            {"origin": "BUD", "destination": "EIN", "departs_local": "2026-09-21T06:15:00+02:00"},
        ],
        stops=1,
        total_minutes=604,
        ground_transfer=False,
        staggered_hours=8.3,
        departs_utc=opens + timedelta(hours=72),
        window_opens_utc=opens,
        window_closes_utc=closes,
    )


def test_want_round_trip(store: Store) -> None:
    want_id = make_want(store, name="Weekend", auto_request_booking=1)
    want = store.get_want(want_id)
    assert want.name == "Weekend"
    assert want.auto_request_booking is True
    assert want.active is True
    assert want.date_from.isoformat() == "2026-09-15"

    store.update_want(want_id, active=0)
    assert store.get_want(want_id).active is False
    assert store.list_wants(active_only=True) == []


def test_deleting_a_want_takes_its_candidates(store: Store) -> None:
    want_id = make_want(store)
    now = datetime.now(UTC)
    make_candidate(store, want_id, opens=now, closes=now + timedelta(hours=60))
    assert len(store.list_candidates()) == 1

    store.delete_want(want_id)
    assert store.list_candidates() == []


def test_candidate_upsert_is_idempotent(store: Store) -> None:
    want_id = make_want(store)
    now = datetime.now(UTC)
    first_id, was_new = make_candidate(store, want_id, opens=now, closes=now + timedelta(hours=60))
    assert was_new

    same_id, was_new_again = make_candidate(
        store, want_id, opens=now, closes=now + timedelta(hours=60)
    )
    assert same_id == first_id
    assert not was_new_again
    assert len(store.list_candidates()) == 1


def test_only_open_candidates_come_up_for_checking(store: Store) -> None:
    want_id = make_want(store)
    now = datetime.now(UTC)

    make_candidate(
        store, want_id, signature="open",
        opens=now - timedelta(hours=1), closes=now + timedelta(hours=40),
    )
    make_candidate(
        store, want_id, signature="future",
        opens=now + timedelta(hours=10), closes=now + timedelta(hours=80),
    )

    due = store.candidates_due_for_check(now)
    assert [c.signature for c in due] == ["open"]


def test_a_paused_want_stops_its_candidates_being_checked(store: Store) -> None:
    want_id = make_want(store)
    now = datetime.now(UTC)
    make_candidate(store, want_id, opens=now - timedelta(hours=1), closes=now + timedelta(hours=40))
    assert store.candidates_due_for_check(now)

    store.update_want(want_id, active=0)
    assert store.candidates_due_for_check(now) == []


def test_recording_a_check_sets_the_next_one(store: Store) -> None:
    want_id = make_want(store)
    now = datetime.now(UTC)
    candidate_id, _ = make_candidate(
        store, want_id, opens=now - timedelta(hours=1), closes=now + timedelta(hours=40)
    )

    later = now + timedelta(minutes=20)
    store.record_check(candidate_id, SOLD_OUT, {"note": "no multipass fare"}, later)

    candidate = store.get_candidate(candidate_id)
    assert candidate.availability == SOLD_OUT
    assert candidate.check_count == 1
    assert store.candidates_due_for_check(now) == []
    assert [c.id for c in store.candidates_due_for_check(later)] == [candidate_id]


def test_expiring_candidates_past_their_window(store: Store) -> None:
    want_id = make_want(store)
    now = datetime.now(UTC)
    make_candidate(
        store, want_id, opens=now - timedelta(hours=80), closes=now - timedelta(hours=1)
    )
    assert store.expire_stale_candidates(now) == 1
    assert store.list_candidates(status=WATCHING) == []
    assert len(store.list_candidates(status=EXPIRED)) == 1


def test_open_booking_guards(store: Store) -> None:
    want_id = make_want(store)
    now = datetime.now(UTC)
    candidate_id, _ = make_candidate(
        store, want_id, opens=now, closes=now + timedelta(hours=60)
    )

    assert not store.has_open_booking(candidate_id)
    booking_id = store.create_booking(candidate_id, "OTP→EIN", {"legs": []})
    assert store.has_open_booking(candidate_id)
    assert store.count_open_bookings() == 1

    store.update_booking(booking_id, status=BOOKED, confirmed_at=now)
    assert not store.has_open_booking(candidate_id)
    assert store.count_open_bookings() == 0
    assert store.count_bookings_since(now - timedelta(hours=1)) == 1


def test_held_bookings_expire(store: Store) -> None:
    want_id = make_want(store)
    now = datetime.now(UTC)
    candidate_id, _ = make_candidate(store, want_id, opens=now, closes=now + timedelta(hours=60))
    booking_id = store.create_booking(candidate_id, "OTP→EIN", {})
    store.update_booking(
        booking_id, status=PENDING_APPROVAL, hold_expires_at=now - timedelta(minutes=1)
    )

    expired = store.expire_held_bookings(now)
    assert [b.id for b in expired] == [booking_id]
    assert store.get_booking(booking_id).status == HOLD_EXPIRED


def test_a_hold_that_has_not_lapsed_survives(store: Store) -> None:
    want_id = make_want(store)
    now = datetime.now(UTC)
    candidate_id, _ = make_candidate(store, want_id, opens=now, closes=now + timedelta(hours=60))
    booking_id = store.create_booking(candidate_id, "OTP→EIN", {})
    store.update_booking(
        booking_id, status=PENDING_APPROVAL, hold_expires_at=now + timedelta(minutes=10)
    )
    assert store.expire_held_bookings(now) == []
    assert store.get_booking(booking_id).status == PENDING_APPROVAL


def test_candidate_window_status(store: Store) -> None:
    want_id = make_want(store)
    now = datetime.now(UTC)
    candidate_id, _ = make_candidate(
        store, want_id, opens=now - timedelta(hours=1), closes=now + timedelta(hours=1)
    )
    candidate = store.get_candidate(candidate_id)
    assert candidate.window_status(now) == "open"
    assert candidate.window_status(now - timedelta(hours=2)) == "too_early"
    assert candidate.window_status(now + timedelta(hours=2)) == "closed"


def test_events_feed(store: Store) -> None:
    store.log("test", "first")
    store.log("test", "second", level="error", candidate_id=7)

    events = store.list_events()
    assert [e["message"] for e in events] == ["second", "first"]
    assert events[0]["level"] == "error"
    assert events[0]["data"]["candidate_id"] == 7

    newest = events[0]["id"]
    assert store.list_events(since_id=newest) == []


def test_candidate_dict_includes_derived_fields(store: Store) -> None:
    want_id = make_want(store, name="Named")
    now = datetime.now(UTC)
    candidate_id, _ = make_candidate(store, want_id, opens=now, closes=now + timedelta(hours=60))
    store.record_check(candidate_id, AVAILABLE, {}, None)

    data = store.get_candidate(candidate_id).to_dict()
    assert data["want_name"] == "Named"
    assert data["trip_credits"] == 2
    assert data["availability"] == AVAILABLE
    assert data["window_status"] == "open"
