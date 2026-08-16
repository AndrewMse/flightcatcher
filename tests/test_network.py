from __future__ import annotations

from datetime import datetime

import pytest

from hopwatch.network import RouteNetwork, estimate_duration_min, haversine_km

from .conftest import build_map


def test_pseudo_mac_station_is_not_a_destination(net: RouteNetwork) -> None:
    """WSW groups WAW and WMI; you cannot fly to the grouping itself."""
    assert "WSW" not in net.stations
    assert not net.has_route("OTP", "WSW")
    assert net.has_route("OTP", "WAW")
    assert net.has_route("OTP", "WMI")


def test_station_names_are_cleaned(net: RouteNetwork) -> None:
    assert net.airport("OTP").name == "Bucharest Otopeni"


def test_resolve_accepts_every_flavour_of_place(net: RouteNetwork) -> None:
    assert net.resolve("EIN") == ["EIN"]
    assert net.resolve("ein") == ["EIN"]
    assert net.resolve("BUH") == ["BBU", "OTP"]  # metro code
    assert net.resolve("Bucharest") == ["BBU", "OTP"]  # city name
    assert net.resolve("NL") == ["EIN"]  # country code
    assert net.resolve("Romania") == ["BBU", "OTP"]  # country name
    assert net.resolve("nowhere-at-all") == []


def test_resolve_city_pulls_in_whole_metro_area(net: RouteNetwork) -> None:
    """'Warsaw Modlin' does not contain 'Chopin', so name matching alone is not enough."""
    assert net.resolve("Warsaw") == ["WAW", "WMI"]


def test_same_metro(net: RouteNetwork) -> None:
    assert net.same_metro("OTP", "BBU")
    assert net.same_metro("WAW", "WAW")
    assert not net.same_metro("OTP", "WAW")
    assert not net.same_metro("EIN", "BUD")  # neither has a MAC


def test_direct_and_one_stop_paths(net: RouteNetwork) -> None:
    paths = net.find_paths(["OTP"], ["EIN"], max_stops=1)
    assert ["OTP", "EIN"] in paths
    assert ["OTP", "WAW", "EIN"] in paths
    assert ["OTP", "BUD", "EIN"] in paths


def test_detour_pruning_rejects_absurd_routings(net: RouteNetwork) -> None:
    """OTP->EVN->EIN flies east to go west; it exists but must be pruned."""
    assert net.has_route("OTP", "EVN") and net.has_route("EVN", "EIN")
    paths = net.find_paths(["OTP"], ["EIN"], max_stops=1)
    assert ["OTP", "EVN", "EIN"] not in paths

    generous = net.find_paths(["OTP"], ["EIN"], max_stops=1, max_detour=10.0)
    assert ["OTP", "EVN", "EIN"] in generous


def test_never_connects_through_the_endpoint_metro(net: RouteNetwork) -> None:
    """Flying OTP -> BBU -> anywhere is not a connection, it is a taxi ride."""
    paths = net.find_paths(["OTP"], ["EIN"], max_stops=1)
    assert all("BBU" not in p for p in paths)


def test_max_stops_is_respected(net: RouteNetwork) -> None:
    assert all(len(p) == 2 for p in net.find_paths(["OTP"], ["EIN"], max_stops=0))

    two = net.find_paths(["OTP"], ["EIN"], max_stops=2, max_detour=10.0)
    assert any(len(p) == 4 for p in two)


def test_routes_not_yet_operating_are_excluded() -> None:
    raw = build_map({"OTP": ["EIN"], "EIN": []})
    raw["cities"][0]["connections"][0]["operationStartDate"] = "2027-03-01T10:00:00"
    net = RouteNetwork(raw)
    assert net.find_paths(["OTP"], ["EIN"], on_or_after=datetime(2026, 9, 15).date()) == []
    assert net.find_paths(["OTP"], ["EIN"], on_or_after=datetime(2027, 6, 1).date()) == [
        ["OTP", "EIN"]
    ]


def test_empty_map_is_rejected() -> None:
    with pytest.raises(ValueError):
        RouteNetwork({"cities": []})


@pytest.mark.parametrize(
    "a,b,expected_km",
    [
        (("OTP", 44.5711, 26.0850), ("EIN", 51.4501, 5.3745), 1713),
        (("OTP", 44.5711, 26.0850), ("BUD", 47.4369, 19.2556), 616),
    ],
)
def test_haversine(a, b, expected_km) -> None:
    got = haversine_km(a[1], a[2], b[1], b[2])
    assert abs(got - expected_km) < 15


def test_duration_estimate_is_in_the_right_ballpark() -> None:
    # Published Wizz block times: OTP-EIN ~2h50, OTP-BUD ~1h15.
    assert 155 <= estimate_duration_min(1713) <= 190
    assert 65 <= estimate_duration_min(616) <= 95
