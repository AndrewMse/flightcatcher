"""Structured logs: one JSON object per line, carrying the job or booking in hand."""

from __future__ import annotations

import io
import json
import logging

import pytest

from hopwatch.logs import configure, log_context


@pytest.fixture
def stream():
    """Configure logging into a buffer, and put the root logger back afterwards."""
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    buffer = io.StringIO()
    yield buffer
    root.handlers[:] = saved[0]
    root.setLevel(saved[1])


def lines(buffer: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buffer.getvalue().splitlines()]


def test_json_line_has_core_fields(stream) -> None:
    configure("json", stream=stream)
    logging.getLogger("hopwatch.test").info("swept %d wants", 3)
    [entry] = lines(stream)
    assert entry["level"] == "INFO"
    assert entry["logger"] == "hopwatch.test"
    assert entry["msg"] == "swept 3 wants"
    assert entry["ts"].endswith("+00:00")


def test_context_fields_attached_and_removed_after_block(stream) -> None:
    configure("json", stream=stream)
    log = logging.getLogger("hopwatch.test")
    with log_context(job="search:1:5", attempt=2):
        log.warning("inside")
    log.warning("outside")
    inside, outside = lines(stream)
    assert inside["job"] == "search:1:5"
    assert inside["attempt"] == 2
    assert "job" not in outside


def test_nested_contexts_merge(stream) -> None:
    configure("json", stream=stream)
    with log_context(worker="w1"):
        with log_context(job="search:1:5"):
            logging.getLogger("t").info("x")
    [entry] = lines(stream)
    assert (entry["worker"], entry["job"]) == ("w1", "search:1:5")


def test_exception_rendered(stream) -> None:
    configure("json", stream=stream)
    try:
        raise ValueError("bad fare")
    except ValueError:
        logging.getLogger("t").exception("check failed")
    [entry] = lines(stream)
    assert "ValueError: bad fare" in entry["exc"]


def test_text_format_unchanged(stream) -> None:
    configure("text", stream=stream)
    with log_context(job="search:1:5"):
        logging.getLogger("hopwatch.x").warning("hello")
    assert stream.getvalue() == "WARNING hopwatch.x: hello\n"


def test_level_is_respected(stream) -> None:
    configure("json", level=logging.WARNING, stream=stream)
    logging.getLogger("t").info("quiet")
    assert stream.getvalue() == ""


def test_worker_logs_carry_job_context(stream, tmp_path, monkeypatch) -> None:
    from hopwatch.client import WizzClient
    from hopwatch.jobs.worker import JobRunner
    from hopwatch.network import RouteNetwork
    from hopwatch.store import SqliteStore

    from .conftest import build_map
    from .contract.conftest import make_want

    def broken(*args, **kwargs):
        raise RuntimeError("HTTP 503")

    monkeypatch.setattr("hopwatch.jobs.worker.sweep_want", broken)
    configure("json", stream=stream)
    store = SqliteStore(tmp_path / "l.db")
    want_id = make_want(store, date_from="2099-01-01", date_to="2099-01-05")
    store.create_job("search:1:7", "search_want", want_id)
    runner = JobRunner(store, WizzClient(cache_dir=tmp_path), lambda: RouteNetwork(build_map()),
                       worker_id="w9", lease_s=60)
    runner.handle({"job": "search:1:7"}, receive_count=1)
    store.close()

    [entry] = [e for e in lines(stream) if "HTTP 503" in e["msg"]]
    assert entry["job"] == "search:1:7"
    assert entry["worker"] == "w9"
    assert entry["attempt"] == 1
    assert entry["want_id"] == want_id
