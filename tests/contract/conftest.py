"""Fixtures that run the same tests against every backend implementation.

A behaviour difference between SQLite and DynamoDB would mean the laptop and
the cloud disagree about whether a job ran or a candidate exists, so the
contract is written once and every implementation must pass all of it.
"""

from __future__ import annotations

import pytest

from hopwatch.jobs.sqlite_queue import SqliteJobQueue
from hopwatch.store import SqliteStore

STORE_BACKENDS = ["sqlite", "dynamo"]
REGION = "eu-central-1"
PREFIX = "hwtest"


@pytest.fixture
def aws(monkeypatch):
    """A moto-mocked AWS account. Nothing here can reach real AWS."""
    moto = pytest.importorskip("moto")
    for name, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": REGION,
    }.items():
        monkeypatch.setenv(name, value)
    with moto.mock_aws():
        yield
QUEUE_BACKENDS = ["sqlite"]


@pytest.fixture(params=STORE_BACKENDS)
def store(request, tmp_path):
    if request.param == "sqlite":
        s = SqliteStore(tmp_path / "contract.db")
    elif request.param == "dynamo":
        request.getfixturevalue("aws")
        from hopwatch.aws.dynamo_store import DynamoStore
        from hopwatch.aws.schema import create_tables

        create_tables(PREFIX, REGION)
        s = DynamoStore(PREFIX, REGION)
    else:  # pragma: no cover
        raise AssertionError(request.param)
    yield s
    s.close()


def make_want(store, **overrides) -> int:
    fields = {
        "name": "Home", "origin": "OTP", "destination": "EIN",
        "date_from": "2026-09-15", "date_to": "2026-09-30", "max_stops": 1,
        "min_layover_min": 180, "max_layover_min": 1200, "max_detour": 2.2,
        "max_trip_hours": 30.0, "allow_ground_transfer": 0, "after_hour": None,
        "before_hour": None, "auto_request_booking": 0, "active": 1, "notes": "",
    }
    fields.update(overrides)
    return store.add_want(**fields)


@pytest.fixture(params=QUEUE_BACKENDS)
def make_queue(request, tmp_path):
    """Build a queue with the given visibility timeout and receive limit."""
    created = []

    def build(visibility_s: float = 30, max_receives: int = 5):
        if request.param == "sqlite":
            q = SqliteJobQueue(
                tmp_path / f"queue{len(created)}.db",
                visibility_s=visibility_s,
                max_receives=max_receives,
            )
        else:  # pragma: no cover - extended as backends are added
            raise AssertionError(request.param)
        created.append(q)
        return q

    yield build
    for q in created:
        q.close()
