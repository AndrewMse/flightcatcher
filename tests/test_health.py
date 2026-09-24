"""Pipeline health: the four signals that mean nobody is getting their flight."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from hopwatch.health import AlertTracker, Signal, evaluate
from hopwatch.jobs.queue import QueueDepth
from hopwatch.store import JOB_DONE, JOB_FAILED, SqliteStore

UTC = timezone.utc
INTERVAL = 15


class FakeQueue:
    def __init__(self, dead: int = 0) -> None:
        self.dead = dead

    def depth(self) -> QueueDepth:
        return QueueDepth(visible=0, in_flight=0, dead=self.dead)


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "h.db")
    yield s
    s.close()


def by_name(signals: list[Signal]) -> dict[str, Signal]:
    return {s.name: s for s in signals}


def test_quiet_pipeline_fires_nothing(store) -> None:
    signals = evaluate(store, FakeQueue(), datetime.now(UTC), INTERVAL)
    assert {s.name for s in signals} == {"dead_letters", "queue_stalled", "job_failures"}
    assert not any(s.firing for s in signals)


def test_dead_letters_fire(store) -> None:
    signal = by_name(evaluate(store, FakeQueue(dead=2), datetime.now(UTC), INTERVAL))["dead_letters"]
    assert signal.firing
    assert "2" in signal.detail


def test_queue_stalled_fires_when_a_job_waits_twice_the_interval(store) -> None:
    store.create_job("search:1:1", "search_want", 1)
    now = datetime.now(UTC)
    assert not by_name(evaluate(store, FakeQueue(), now, INTERVAL))["queue_stalled"].firing
    later = now + timedelta(minutes=2 * INTERVAL + 1)
    stalled = by_name(evaluate(store, FakeQueue(), later, INTERVAL))["queue_stalled"]
    assert stalled.firing
    assert "search:1:1" in stalled.detail


def test_resending_a_job_does_not_hide_that_it_is_stalled(store) -> None:
    store.create_job("search:1:1", "search_want", 1)
    later = datetime.now(UTC) + timedelta(minutes=2 * INTERVAL + 1)
    store.touch_job("search:1:1", later)
    assert by_name(evaluate(store, FakeQueue(), later, INTERVAL))["queue_stalled"].firing


def test_job_failures_need_four_jobs(store) -> None:
    for i, status in enumerate([JOB_FAILED, JOB_FAILED, JOB_DONE]):
        store.create_job(f"search:1:{i}", "search_want", 1)
        store.finish_job(f"search:1:{i}", status)
    now = datetime.now(UTC)
    assert not by_name(evaluate(store, FakeQueue(), now, INTERVAL))["job_failures"].firing

    store.create_job("search:1:9", "search_want", 1)
    store.finish_job("search:1:9", JOB_FAILED)
    failing = by_name(evaluate(store, FakeQueue(), now, INTERVAL))["job_failures"]
    assert failing.firing
    assert "3 of 4" in failing.detail


def test_mostly_successful_jobs_do_not_fire(store) -> None:
    for i, status in enumerate([JOB_DONE, JOB_DONE, JOB_DONE, JOB_FAILED]):
        store.create_job(f"search:1:{i}", "search_want", 1)
        store.finish_job(f"search:1:{i}", status)
    assert not by_name(evaluate(store, FakeQueue(), datetime.now(UTC), INTERVAL))["job_failures"].firing


def test_booker_silent_only_when_asked(store) -> None:
    now = datetime.now(UTC)
    assert "booker_silent" not in by_name(evaluate(store, FakeQueue(), now, INTERVAL))
    silent = by_name(evaluate(store, FakeQueue(), now, INTERVAL, include_booker=True))
    assert silent["booker_silent"].firing  # never reported at all

    store.beat("booker", now=now - timedelta(minutes=5))
    fresh = by_name(evaluate(store, FakeQueue(), now, INTERVAL, include_booker=True))
    assert not fresh["booker_silent"].firing

    store.beat("booker", now=now - timedelta(minutes=16))
    stale = by_name(evaluate(store, FakeQueue(), now, INTERVAL, include_booker=True))
    assert stale["booker_silent"].firing


def test_tracker_alerts_once_per_incident_and_reports_recovery() -> None:
    tracker = AlertTracker()
    down = [Signal("dead_letters", True, "1 dead"), Signal("queue_stalled", False, "")]
    up = [Signal("dead_letters", False, ""), Signal("queue_stalled", False, "")]

    assert [s.name for s in tracker.update(down)[0]] == ["dead_letters"]
    assert tracker.update(down) == ([], [])
    fired, recovered = tracker.update(up)
    assert fired == [] and [s.name for s in recovered] == ["dead_letters"]
    assert tracker.update(up) == ([], [])
