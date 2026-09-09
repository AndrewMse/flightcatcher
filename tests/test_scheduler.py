"""Scheduling sweeps: one job per want per slot, however often we are asked."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from hopwatch.jobs.scheduler import (
    enqueue_manual,
    enqueue_sweep,
    manual_key,
    resend_stale,
    slot_key,
)
from hopwatch.jobs.sqlite_queue import SqliteJobQueue
from hopwatch.store import JOB_QUEUED, SqliteStore

from .contract.conftest import make_want

UTC = timezone.utc
NOW = datetime(2026, 9, 9, 12, 7, 30, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "s.db")
    yield s
    s.close()


@pytest.fixture
def queue(tmp_path):
    q = SqliteJobQueue(tmp_path / "s.db")
    yield q
    q.close()


def drain(queue) -> list[str]:
    keys = []
    while batch := queue.receive(max_messages=10):
        for message in batch:
            keys.append(message.body["job"])
            queue.ack(message)
    return keys


def test_slot_key_is_stable_within_interval() -> None:
    assert slot_key(4, NOW, 15) == slot_key(4, NOW + timedelta(minutes=7), 15)
    assert slot_key(4, NOW, 15) != slot_key(4, NOW + timedelta(minutes=15), 15)
    assert slot_key(4, NOW, 15).startswith("search:4:")


def test_manual_key_changes_each_minute() -> None:
    assert manual_key(4, NOW) == manual_key(4, NOW + timedelta(seconds=20))
    assert manual_key(4, NOW) != manual_key(4, NOW + timedelta(minutes=1))
    assert manual_key(4, NOW).startswith("manual:4:")


def test_enqueue_sweep_twice_in_one_slot_sends_once(store, queue) -> None:
    make_want(store, name="a")
    make_want(store, name="b")
    assert enqueue_sweep(store, queue, NOW, 15) == 2
    assert enqueue_sweep(store, queue, NOW + timedelta(minutes=1), 15) == 0
    assert len(drain(queue)) == 2
    assert enqueue_sweep(store, queue, NOW + timedelta(minutes=15), 15) == 2


def test_enqueue_skips_inactive_wants(store, queue) -> None:
    make_want(store, name="on")
    make_want(store, name="off", active=0)
    assert enqueue_sweep(store, queue, NOW, 15) == 1


def test_manual_double_click_creates_one_job(store, queue) -> None:
    want_id = make_want(store)
    key, created = enqueue_manual(store, queue, want_id, NOW)
    again, created_again = enqueue_manual(store, queue, want_id, NOW + timedelta(seconds=5))
    assert (created, created_again) == (True, False)
    assert key == again
    assert drain(queue) == [key]


def test_resend_stale_requeues_only_old_queued(store, queue) -> None:
    want_id = make_want(store)
    store.create_job("search:1:1", "search_want", want_id)  # lost its message
    store.create_job("search:1:2", "search_want", want_id)
    store.claim_job("search:1:2", "w", lease_s=60)  # being worked on

    later = datetime.now(UTC) + timedelta(minutes=20)
    assert resend_stale(store, queue, later, older_than_s=900) == 1
    assert drain(queue) == ["search:1:1"]
    assert store.get_job("search:1:1").status == JOB_QUEUED
    # Resent once per period, not on every tick.
    assert resend_stale(store, queue, later + timedelta(seconds=1), older_than_s=900) == 0
