"""Fixtures that run the same tests against every backend implementation.

A behaviour difference between SQLite and DynamoDB would mean the laptop and
the cloud disagree about whether a job ran or a candidate exists, so the
contract is written once and every implementation must pass all of it.
"""

from __future__ import annotations

import pytest

from hopwatch.store import SqliteStore

STORE_BACKENDS = ["sqlite"]


@pytest.fixture(params=STORE_BACKENDS)
def store(request, tmp_path):
    if request.param == "sqlite":
        s = SqliteStore(tmp_path / "contract.db")
    else:  # pragma: no cover - extended as backends are added
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
