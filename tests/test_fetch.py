"""Partial-failure behaviour of the fan-out over legs."""

from __future__ import annotations

from datetime import date

from flightcatcher import config
from flightcatcher.client import WizzError
from flightcatcher.network import RouteNetwork
from flightcatcher.search import fetch_departures


class FakeClient:
    """Answers for some legs, refuses others."""

    def __init__(self, broken: set[tuple[str, str]]) -> None:
        self.broken = broken
        self.asked: list[tuple[str, str]] = []

    def timetable(self, origin, destination, date_from, date_to, refresh=False):
        self.asked.append((origin, destination))
        if (origin, destination) in self.broken:
            raise WizzError("backend said no")
        return [
            {
                "departureStation": origin,
                "arrivalStation": destination,
                "departureDates": ["2026-09-16T08:00:00"],
                "priceType": "price",
            }
        ]


def test_failed_legs_are_reported_not_silently_empty(net: RouteNetwork, monkeypatch) -> None:
    monkeypatch.setattr(config, "MAX_CONCURRENCY", 1)
    client = FakeClient(broken={("OTP", "WAW")})

    pairs = [("OTP", "WAW"), ("WAW", "EIN"), ("OTP", "EIN")]
    results, failed = fetch_departures(
        client, net, pairs, date(2026, 9, 15), date(2026, 9, 22)
    )

    assert failed == [("OTP", "WAW")]
    assert ("OTP", "WAW") not in results
    assert len(results[("WAW", "EIN")]) == 1
    assert len(results[("OTP", "EIN")]) == 1


def test_all_good_legs_report_no_failures(net: RouteNetwork, monkeypatch) -> None:
    monkeypatch.setattr(config, "MAX_CONCURRENCY", 1)
    client = FakeClient(broken=set())
    results, failed = fetch_departures(
        client, net, [("OTP", "EIN")], date(2026, 9, 15), date(2026, 9, 22)
    )
    assert failed == []
    assert len(results) == 1


def test_no_pairs_is_not_an_error(net: RouteNetwork) -> None:
    results, failed = fetch_departures(
        FakeClient(set()), net, [], date(2026, 9, 15), date(2026, 9, 22)
    )
    assert results == {} and failed == []
