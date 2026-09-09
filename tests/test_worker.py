"""The job runner: every way a queued search can arrive, fail, or repeat."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from hopwatch.client import WizzClient
from hopwatch.jobs.sqlite_queue import SqliteJobQueue
from hopwatch.jobs.worker import JobRunner, backoff_s, run_worker
from hopwatch.network import RouteNetwork
from hopwatch.store import (
    JOB_DEAD,
    JOB_DONE,
    JOB_FAILED,
    JOB_RETRYING,
    RUN_ERROR,
    RUN_INCOMPLETE,
    RUN_OK,
    SqliteStore,
)
from hopwatch.sweep import PermanentJobError, SweepResult

from .conftest import build_map

UTC = timezone.utc
KEY = "search:1:100"
OK = SweepResult(itineraries=3, new_candidates=2, complete=True, failed_routes=[],
                 paths_considered=4, routes_queried=5)


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "w.db")
    yield s
    s.close()


@pytest.fixture
def calls(monkeypatch):
    """Replace the real sweep; each test sets what it returns or raises."""
    record = {"n": 0, "result": OK}

    def fake(store, client, network, want, today=None):
        record["n"] += 1
        outcome = record["result"]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr("hopwatch.jobs.worker.sweep_want", fake)
    return record


def add_want(store, **overrides) -> int:
    fields = {
        "name": "Home", "origin": "OTP", "destination": "EIN",
        "date_from": date.today().isoformat(),
        "date_to": (date.today() + timedelta(days=5)).isoformat(), "max_stops": 1,
        "min_layover_min": 180, "max_layover_min": 1200, "max_detour": 2.2,
        "max_trip_hours": 30.0, "allow_ground_transfer": 0, "after_hour": None,
        "before_hour": None, "auto_request_booking": 0, "active": 1, "notes": "",
    }
    fields.update(overrides)
    return store.add_want(**fields)


def runner_for(store, tmp_path, worker_id: str = "w1") -> JobRunner:
    return JobRunner(
        store,
        WizzClient(cache_dir=tmp_path / "cache"),
        network=lambda: RouteNetwork(build_map()),
        worker_id=worker_id,
        lease_s=60,
        max_receives=5,
    )


@pytest.fixture
def job(store) -> str:
    want_id = add_want(store)
    store.create_job(KEY, "search_want", want_id)
    return KEY


def test_backoff_doubles_and_caps() -> None:
    assert [backoff_s(n) for n in (1, 2, 3, 6, 10)] == [30, 60, 120, 900, 900]


def test_happy_path_marks_done_and_records_run(store, tmp_path, job, calls) -> None:
    outcome = runner_for(store, tmp_path).handle({"job": job}, receive_count=1)
    assert outcome.action == "ack"
    done = store.get_job(job)
    assert done.status == JOB_DONE
    assert done.result["new_candidates"] == 2
    assert done.result["complete"] is True
    [run] = store.list_search_runs()
    assert run.status == RUN_OK
    assert (run.itineraries, run.new_candidates, run.routes_queried) == (3, 2, 5)


def test_duplicate_delivery_after_done_is_acked_without_work(store, tmp_path, job, calls) -> None:
    runner = runner_for(store, tmp_path)
    runner.handle({"job": job}, receive_count=1)
    again = runner.handle({"job": job}, receive_count=1)
    assert again.action == "ack"
    assert again.reason == "duplicate"
    assert calls["n"] == 1
    assert len(store.list_search_runs()) == 1


def test_crash_mid_job_is_taken_over_after_lease(store, tmp_path, job, calls) -> None:
    long_ago = datetime.now(UTC) - timedelta(minutes=5)
    store.claim_job(job, "crashed-worker", lease_s=60, now=long_ago)

    outcome = runner_for(store, tmp_path, "w2").handle({"job": job}, receive_count=2)
    assert outcome.action == "ack"
    done = store.get_job(job)
    assert done.status == JOB_DONE
    assert done.worker == "w2"
    assert done.attempts == 2
    assert [r.status for r in store.list_search_runs()] == [RUN_OK]


def test_live_lease_defers_instead_of_running_twice(store, tmp_path, job, calls) -> None:
    store.claim_job(job, "busy-worker", lease_s=60)
    outcome = runner_for(store, tmp_path).handle({"job": job}, receive_count=2)
    assert outcome.action == "retry"
    assert 50 <= outcome.delay_s <= 60
    assert calls["n"] == 0
    assert store.get_job(job).worker == "busy-worker"


def test_transient_error_retries_with_backoff(store, tmp_path, job, calls) -> None:
    calls["result"] = RuntimeError("HTTP 503")
    runner = runner_for(store, tmp_path)

    first = runner.handle({"job": job}, receive_count=1)
    assert (first.action, first.delay_s) == ("retry", 30)
    failed = store.get_job(job)
    assert failed.status == JOB_RETRYING
    assert "HTTP 503" in failed.last_error
    assert [r.status for r in store.list_search_runs()] == [RUN_ERROR]

    second = runner.handle({"job": job}, receive_count=2)
    assert (second.action, second.delay_s) == ("retry", 60)


def test_last_receive_marks_dead(store, tmp_path, job, calls) -> None:
    calls["result"] = RuntimeError("still broken")
    outcome = runner_for(store, tmp_path).handle({"job": job}, receive_count=5)
    assert outcome.action == "retry"
    assert store.get_job(job).status == JOB_DEAD


def test_unresolvable_place_fails_permanently(store, tmp_path, job, calls) -> None:
    calls["result"] = PermanentJobError("could not resolve 'Narnia'")
    outcome = runner_for(store, tmp_path).handle({"job": job}, receive_count=1)
    assert outcome.action == "ack"
    failed = store.get_job(job)
    assert failed.status == JOB_FAILED
    assert "Narnia" in failed.last_error


def test_deleted_want_fails_permanently(store, tmp_path, job, calls) -> None:
    store.delete_want(store.get_job(job).want_id)
    outcome = runner_for(store, tmp_path).handle({"job": job}, receive_count=1)
    assert outcome.action == "ack"
    assert store.get_job(job).status == JOB_FAILED
    assert calls["n"] == 0


def test_paused_want_fails_permanently(store, tmp_path, job, calls) -> None:
    store.update_want(store.get_job(job).want_id, active=0)
    outcome = runner_for(store, tmp_path).handle({"job": job}, receive_count=1)
    assert outcome.action == "ack"
    assert store.get_job(job).status == JOB_FAILED
    assert calls["n"] == 0


def test_want_deleted_mid_sweep_fails_permanently(store, tmp_path, job, calls) -> None:
    calls["result"] = sqlite3.IntegrityError("FOREIGN KEY constraint failed")
    outcome = runner_for(store, tmp_path).handle({"job": job}, receive_count=1)
    assert outcome.action == "ack"
    assert store.get_job(job).status == JOB_FAILED


@pytest.mark.parametrize("body", ["not json", "{}", '{"job": 7}', {"other": 1}, {}])
def test_malformed_body_is_acked(store, tmp_path, calls, body) -> None:
    outcome = runner_for(store, tmp_path).handle(body, receive_count=1)
    assert outcome.action == "ack"
    assert outcome.reason == "malformed"
    assert calls["n"] == 0


def test_json_string_body_is_accepted(store, tmp_path, job, calls) -> None:
    outcome = runner_for(store, tmp_path).handle('{"job": "%s"}' % job, receive_count=1)
    assert outcome.action == "ack"
    assert store.get_job(job).status == JOB_DONE


def test_unknown_job_is_acked(store, tmp_path, calls) -> None:
    outcome = runner_for(store, tmp_path).handle({"job": "search:9:9"}, receive_count=1)
    assert outcome.action == "ack"
    assert calls["n"] == 0


def test_incomplete_sweep_is_done_but_flagged(store, tmp_path, job, calls) -> None:
    calls["result"] = SweepResult(itineraries=1, new_candidates=1, complete=False,
                                  failed_routes=["OTP→WAW"], paths_considered=2,
                                  routes_queried=3)
    runner_for(store, tmp_path).handle({"job": job}, receive_count=1)
    done = store.get_job(job)
    assert done.status == JOB_DONE
    assert done.result["complete"] is False
    [run] = store.list_search_runs()
    assert run.status == RUN_INCOMPLETE
    assert run.failed_routes == ["OTP→WAW"]


async def test_run_worker_drains_queue(store, tmp_path, calls) -> None:
    want_id = add_want(store)
    queue = SqliteJobQueue(tmp_path / "w.db")
    for i in range(3):
        key = f"search:{want_id}:{i}"
        store.create_job(key, "search_want", want_id)
        queue.send({"job": key})

    stop = asyncio.Event()
    task = asyncio.create_task(run_worker(queue, runner_for(store, tmp_path), stop, idle_s=0.05))
    for _ in range(100):
        if all(store.get_job(f"search:{want_id}:{i}").status == JOB_DONE for i in range(3)):
            break
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=5)

    assert calls["n"] == 3
    assert queue.depth().visible == 0 and queue.depth().in_flight == 0
    assert "worker:w1" in store.heartbeats()
    queue.close()


async def test_run_worker_retries_failed_jobs_later(store, tmp_path, job, calls) -> None:
    calls["result"] = RuntimeError("boom")
    queue = SqliteJobQueue(tmp_path / "w.db")
    queue.send({"job": job})
    stop = asyncio.Event()
    task = asyncio.create_task(run_worker(queue, runner_for(store, tmp_path), stop, idle_s=0.05))
    for _ in range(100):
        if store.get_job(job).status == JOB_RETRYING:
            break
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    depth = queue.depth()
    assert (depth.visible, depth.in_flight) == (0, 1)  # hidden for the backoff
    queue.close()


def test_network_loader_caches_until_ttl() -> None:
    from hopwatch.jobs.worker import network_loader

    fetches: list[int] = []

    class Client:
        def route_map(self):
            fetches.append(1)
            return build_map()

    clock = [0.0]
    load = network_loader(Client(), ttl_s=100, clock=lambda: clock[0])
    first, second = load(), load()
    assert first is second
    assert len(fetches) == 1
    clock[0] = 101
    load()
    assert len(fetches) == 2
