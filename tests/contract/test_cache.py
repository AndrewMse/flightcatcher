"""The response cache, on local disk and in DynamoDB."""

from __future__ import annotations

import json

import pytest

from hopwatch.cache import DiskCache

from .conftest import PREFIX, REGION


@pytest.fixture(params=["disk", "dynamo"])
def cache(request, tmp_path):
    if request.param == "disk":
        return DiskCache(tmp_path / "cache")
    request.getfixturevalue("aws")
    from hopwatch.aws.cache import DynamoCache
    from hopwatch.aws.schema import create_tables

    create_tables(PREFIX, REGION)
    return DynamoCache(PREFIX, REGION)


def test_round_trip(cache) -> None:
    cache.set("tt_OTP_EIN_2026-09-01_2026-09-10", [{"departureDates": ["2026-09-01T06:00:00"]}])
    assert cache.get("tt_OTP_EIN_2026-09-01_2026-09-10", ttl=60) == [
        {"departureDates": ["2026-09-01T06:00:00"]}
    ]


def test_missing_key(cache) -> None:
    assert cache.get("nope", ttl=60) is None
    assert cache.get_stale("nope") is None


def test_ttl_expiry_leaves_a_stale_copy(cache) -> None:
    cache.set("k", {"v": 1})
    assert cache.get("k", ttl=-1) is None
    assert cache.get_stale("k") == {"v": 1}


def test_overwrite(cache) -> None:
    cache.set("k", 1)
    cache.set("k", 2)
    assert cache.get("k", ttl=60) == 2


def test_a_full_route_map_fits(cache) -> None:
    """The real map is a few hundred KB of JSON; DynamoDB items cap at 400 KB."""
    cities = [
        {
            "iata": f"A{i:02d}", "shortName": f"Airport number {i}", "countryCode": "XX",
            "countryName": "Somewhere", "latitude": 40.0 + i / 10, "longitude": 10.0 + i / 10,
            "connections": [{"iata": f"A{j:02d}", "operationStartDate": None,
                             "isDirectFlight": True} for j in range(60)],
        }
        for i in range(200)
    ]
    route_map = {"cities": cities}
    assert len(json.dumps(route_map)) > 400_000
    cache.set("route_map", route_map)
    assert cache.get("route_map", ttl=60) == route_map
