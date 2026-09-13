"""Job records: the idempotency and lease rules every worker relies on."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from hopwatch.store import (
    JOB_DEAD,
    JOB_DONE,
    JOB_FAILED,
    JOB_QUEUED,
    JOB_RETRYING,
    JOB_RUNNING,
    RUN_INCOMPLETE,
    RUN_RUNNING,
)

UTC = timezone.utc
KEY = "search:1:5"


def test_create_job_is_idempotent(store) -> None:
    assert store.create_job(KEY, "search_want", 1) is True
    assert store.create_job(KEY, "search_want", 1) is False
    job = store.get_job(KEY)
    assert job.status == JOB_QUEUED
    assert job.want_id == 1
    assert job.attempts == 0


def test_unknown_job_is_none(store) -> None:
    assert store.get_job("search:404:1") is None


def test_claim_increments_attempts_and_sets_lease(store) -> None:
    store.create_job(KEY, "search_want", 1)
    now = datetime.now(UTC)
    job = store.claim_job(KEY, "w1", lease_s=60, now=now)
    assert job is not None
    assert job.status == JOB_RUNNING
    assert job.worker == "w1"
    assert job.attempts == 1
    assert abs((job.lease_until - (now + timedelta(seconds=60))).total_seconds()) < 1


def test_claim_refused_while_lease_live(store) -> None:
    store.create_job(KEY, "search_want", 1)
    now = datetime.now(UTC)
    assert store.claim_job(KEY, "w1", lease_s=60, now=now) is not None
    assert store.claim_job(KEY, "w2", lease_s=60, now=now + timedelta(seconds=30)) is None
    assert store.get_job(KEY).worker == "w1"


def test_claim_takes_over_expired_lease(store) -> None:
    store.create_job(KEY, "search_want", 1)
    now = datetime.now(UTC)
    store.claim_job(KEY, "w1", lease_s=60, now=now)
    job = store.claim_job(KEY, "w2", lease_s=60, now=now + timedelta(seconds=61))
    assert job is not None
    assert job.worker == "w2"
    assert job.attempts == 2


@pytest.mark.parametrize("status", [JOB_DONE, JOB_FAILED, JOB_DEAD])
def test_claim_refused_for_finished_jobs(store, status) -> None:
    store.create_job(KEY, "search_want", 1)
    store.claim_job(KEY, "w1", lease_s=60)
    store.finish_job(KEY, status, result={"x": 1}, error="why" if status != JOB_DONE else None)
    later = datetime.now(UTC) + timedelta(hours=1)
    assert store.claim_job(KEY, "w2", lease_s=60, now=later) is None
    job = store.get_job(KEY)
    assert job.status == status
    assert job.finished_at is not None
    assert job.result == {"x": 1}


def test_release_then_reclaim(store) -> None:
    store.create_job(KEY, "search_want", 1)
    store.claim_job(KEY, "w1", lease_s=60)
    store.release_job(KEY, "HTTP 503")
    job = store.get_job(KEY)
    assert job.status == JOB_RETRYING
    assert job.last_error == "HTTP 503"
    assert job.worker is None
    assert job.lease_until is None
    again = store.claim_job(KEY, "w2", lease_s=60)
    assert again is not None and again.attempts == 2


def test_list_jobs_filters_by_status(store) -> None:
    store.create_job("search:1:1", "search_want", 1)
    store.create_job("search:1:2", "search_want", 1)
    store.claim_job("search:1:2", "w", lease_s=60)
    assert [j.key for j in store.list_jobs(status=JOB_QUEUED)] == ["search:1:1"]
    assert {j.key for j in store.list_jobs()} == {"search:1:1", "search:1:2"}


def test_stale_jobs_returns_old_queued_only(store) -> None:
    store.create_job("search:1:1", "search_want", 1)
    store.create_job("search:1:2", "search_want", 1)
    store.claim_job("search:1:2", "w", lease_s=60)
    future = datetime.now(UTC) + timedelta(minutes=30)
    assert [j.key for j in store.stale_jobs(older_than=future)] == ["search:1:1"]
    past = datetime.now(UTC) - timedelta(minutes=30)
    assert store.stale_jobs(older_than=past) == []


def test_job_outcomes_since(store) -> None:
    for i, status in enumerate([JOB_DONE, JOB_DONE, JOB_FAILED]):
        key = f"search:1:{i}"
        store.create_job(key, "search_want", 1)
        store.finish_job(key, status)
    store.create_job("search:1:9", "search_want", 1)  # unfinished: not counted
    since = datetime.now(UTC) - timedelta(minutes=5)
    assert store.job_outcomes_since(since) == {JOB_DONE: 2, JOB_FAILED: 1}
    assert store.job_outcomes_since(datetime.now(UTC) + timedelta(minutes=5)) == {}


def test_search_run_round_trip(store) -> None:
    run_id = store.start_search_run(want_id=3, job_key=KEY)
    started = store.list_search_runs(want_id=3)[0]
    assert started.id == run_id
    assert started.status == RUN_RUNNING
    assert started.finished_at is None

    store.finish_search_run(
        run_id, 3, RUN_INCOMPLETE, paths_considered=4, routes_queried=6,
        upstream_calls=5, cache_hits=1, itineraries=9, new_candidates=2,
        failed_routes=["OTP→WAW"],
    )
    run = store.list_search_runs(want_id=3)[0]
    assert run.status == RUN_INCOMPLETE
    assert run.job_key == KEY
    assert (run.paths_considered, run.routes_queried, run.upstream_calls) == (4, 6, 5)
    assert (run.cache_hits, run.itineraries, run.new_candidates) == (1, 9, 2)
    assert run.failed_routes == ["OTP→WAW"]
    assert run.duration_ms is not None and run.duration_ms >= 0
    assert store.list_search_runs(want_id=99) == []


def test_search_runs_newest_first(store) -> None:
    first = store.start_search_run(want_id=1, job_key=None)
    second = store.start_search_run(want_id=2, job_key=None)
    assert [r.id for r in store.list_search_runs()] == [second, first]


def test_heartbeats_round_trip(store) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    store.beat("booker", now=now)
    store.beat("worker:a", now=now - timedelta(minutes=1))
    beats = store.heartbeats()
    assert beats["booker"] == now
    assert beats["worker:a"] == now - timedelta(minutes=1)


def test_only_one_of_many_concurrent_claims_wins(store) -> None:
    from concurrent.futures import ThreadPoolExecutor

    store.create_job(KEY, "search_want", 1)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda i: store.claim_job(KEY, f"w{i}", lease_s=60), range(8)))
    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    assert store.get_job(KEY).attempts == 1
