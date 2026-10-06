"""Lambda entry points, against moto-mocked DynamoDB and SQS."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone

import boto3
import pytest

from hopwatch.network import RouteNetwork
from hopwatch.store import JOB_DONE, JOB_RETRYING
from hopwatch.sweep import SweepResult

from .conftest import build_map
from .contract.conftest import PREFIX, REGION, create_sqs_pair

UTC = timezone.utc


@pytest.fixture
def lambdas(aws, monkeypatch):
    from hopwatch.aws import lambdas as module
    from hopwatch.aws.schema import create_tables

    create_tables(PREFIX, REGION)
    queue_url, dlq_url = create_sqs_pair("searches", visibility_s=900)
    for name, value in {
        "HOPWATCH_BACKEND": "aws",
        "HOPWATCH_TABLE_PREFIX": PREFIX,
        "HOPWATCH_AWS_REGION": REGION,
        "HOPWATCH_QUEUE_URL": queue_url,
        "HOPWATCH_DLQ_URL": dlq_url,
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(module, "network_loader", lambda client: lambda: RouteNetwork(build_map()))
    module._state.clear()
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    yield module
    module._state.clear()
    root.handlers[:], _ = saved[0], root.setLevel(saved[1])


@pytest.fixture
def sweeps(monkeypatch):
    record = {"n": 0, "result": SweepResult(itineraries=1, new_candidates=1, complete=True)}

    def fake(store, client, network, want, today=None):
        record["n"] += 1
        if isinstance(record["result"], BaseException):
            raise record["result"]
        return record["result"]

    monkeypatch.setattr("hopwatch.jobs.worker.sweep_want", fake)
    return record


def add_want(store) -> int:
    return store.add_want(
        name="Home", origin="OTP", destination="EIN", date_from=date.today().isoformat(),
        date_to=(date.today() + timedelta(days=3)).isoformat(), max_stops=1,
        min_layover_min=180, max_layover_min=1200, max_detour=2.2, max_trip_hours=30.0,
        allow_ground_transfer=0, after_hour=None, before_hour=None,
        auto_request_booking=0, active=1, notes="",
    )


def sqs_event(queue_url: str) -> dict:
    """Receive like the Lambda event source mapping does, and wrap it the same way."""
    sqs = boto3.client("sqs", region_name=REGION)
    messages = sqs.receive_message(
        QueueUrl=queue_url, MaxNumberOfMessages=1, AttributeNames=["All"]
    ).get("Messages", [])
    return {
        "Records": [
            {
                "messageId": m["MessageId"],
                "receiptHandle": m["ReceiptHandle"],
                "body": m["Body"],
                "attributes": {
                    "ApproximateReceiveCount": m["Attributes"]["ApproximateReceiveCount"],
                    "SentTimestamp": m["Attributes"]["SentTimestamp"],
                },
            }
            for m in messages
        ]
    }


def test_planner_enqueues_once_per_slot(lambdas, sweeps, monkeypatch) -> None:
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 9, 12, 0, 5, tzinfo=UTC)

    monkeypatch.setattr(lambdas, "datetime", Frozen)  # both calls in one 15-min slot
    store = lambdas.backend().store
    add_want(store)
    first = lambdas.planner_handler({}, None)
    second = lambdas.planner_handler({}, None)
    assert first["enqueued"] == 1
    assert second["enqueued"] == 0
    assert lambdas.backend().queue.depth().visible == 1


def test_worker_handler_runs_the_job(lambdas, sweeps) -> None:
    store = lambdas.backend().store
    add_want(store)
    lambdas.planner_handler({}, None)
    [job] = store.list_jobs()

    result = lambdas.worker_handler(sqs_event(lambdas.backend().queue.queue_url), None)
    assert result == {"batchItemFailures": []}
    assert store.get_job(job.key).status == JOB_DONE
    assert sweeps["n"] == 1


def test_worker_handler_acks_duplicates(lambdas, sweeps) -> None:
    store = lambdas.backend().store
    add_want(store)
    lambdas.planner_handler({}, None)
    event = sqs_event(lambdas.backend().queue.queue_url)
    lambdas.worker_handler(event, None)
    assert lambdas.worker_handler(event, None) == {"batchItemFailures": []}
    assert sweeps["n"] == 1


def test_worker_handler_reports_failures_for_retries(lambdas, sweeps) -> None:
    store = lambdas.backend().store
    add_want(store)
    lambdas.planner_handler({}, None)
    [job] = store.list_jobs()
    sweeps["result"] = RuntimeError("HTTP 503")

    event = sqs_event(lambdas.backend().queue.queue_url)
    result = lambdas.worker_handler(event, None)
    assert result == {"batchItemFailures": [{"itemIdentifier": event["Records"][0]["messageId"]}]}
    assert store.get_job(job.key).status == JOB_RETRYING


def test_worker_handler_survives_a_malformed_record(lambdas, sweeps) -> None:
    event = {"Records": [{"messageId": "m1", "receiptHandle": "r1", "body": "{not json",
                          "attributes": {"ApproximateReceiveCount": "1", "SentTimestamp": "0"}}]}
    assert lambdas.worker_handler(event, None) == {"batchItemFailures": []}


def test_planner_reports_booker_heartbeat_age(lambdas, sweeps, capsys) -> None:
    store = lambdas.backend().store
    store.beat("booker", now=datetime.now(UTC) - timedelta(seconds=120))
    result = lambdas.planner_handler({}, None)
    assert 115 <= result["booker_heartbeat_age_s"] <= 130

    emf = [json.loads(line) for line in capsys.readouterr().out.splitlines() if '"_aws"' in line]
    assert len(emf) == 1
    assert emf[0]["_aws"]["CloudWatchMetrics"][0]["Namespace"] == "Hopwatch"
    assert 115 <= emf[0]["BookerHeartbeatAgeSeconds"] <= 130


def test_planner_without_a_booker_emits_no_heartbeat_metric(lambdas, sweeps, capsys) -> None:
    result = lambdas.planner_handler({}, None)
    assert result["booker_heartbeat_age_s"] is None
    assert '"_aws"' not in capsys.readouterr().out
