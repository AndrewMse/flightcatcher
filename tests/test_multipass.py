"""Layer 3 parsing and pacing.

The browser half cannot be exercised without a real Multipass account, so what
is tested here is everything that sits either side of it: how a search response
is interpreted, and how hard the checker is allowed to push.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from hopwatch.multipass import (
    CheckBudget,
    _fare_is_multipass,
    next_check_time,
    parse_leg,
)
from hopwatch.settings import WatcherSettings
from hopwatch.store import AVAILABLE, SOLD_OUT

UTC = timezone.utc


def response(fares, departure="2026-09-20T06:50:00", arrival="2026-09-20T08:40:00"):
    return {
        "outboundFlights": [
            {
                "departureStation": "OTP",
                "arrivalStation": "EIN",
                "departureDateTime": departure,
                "arrivalDateTime": arrival,
                "flightNumber": "W64321",
                "fares": fares,
            }
        ]
    }


# --- fare classification ----------------------------------------------------


@pytest.mark.parametrize(
    "fare",
    [
        {"bundle": "MULTIPASS"},
        {"fareType": "multipass"},
        {"name": "Wizz MultiPass"},
        {"fareSellKey": "X~MULTI_PASS~1"},
        {"priceType": "multipass"},
    ],
)
def test_multipass_fares_are_recognised(fare) -> None:
    assert _fare_is_multipass(fare)


@pytest.mark.parametrize(
    "fare",
    [{"bundle": "BASIC"}, {"bundle": "WIZZ_PLUS"}, {"fareType": "standard"}, {}],
)
def test_ordinary_fares_are_not(fare) -> None:
    assert not _fare_is_multipass(fare)


# --- leg parsing ------------------------------------------------------------


def test_multipass_fare_present_means_available() -> None:
    leg = parse_leg(
        response([{"bundle": "BASIC"}, {"bundle": "MULTIPASS", "availableCount": 4}]),
        "OTP", "EIN", "2026-09-20T06:50:00+03:00",
    )
    assert leg.found
    assert leg.multipass is True
    assert leg.seats == 4
    assert leg.signal == "multipass_fare_present"
    assert leg.flight_number == "W64321"


def test_real_arrival_time_is_captured() -> None:
    """The public timetable has no arrivals; this response does."""
    leg = parse_leg(
        response([{"bundle": "MULTIPASS"}]), "OTP", "EIN", "2026-09-20T06:50:00+03:00"
    )
    assert leg.arrives_local_actual == "2026-09-20T08:40"


def test_fares_without_multipass_mean_sold_out() -> None:
    leg = parse_leg(
        response([{"bundle": "BASIC"}, {"bundle": "WIZZ_PLUS"}]),
        "OTP", "EIN", "2026-09-20T06:50:00+03:00",
    )
    assert leg.found
    assert leg.multipass is False
    assert leg.signal == "fares_listed_without_multipass"


def test_zero_seats_overrides_a_listed_fare() -> None:
    leg = parse_leg(
        response([{"bundle": "MULTIPASS", "availableCount": 0}]),
        "OTP", "EIN", "2026-09-20T06:50:00+03:00",
    )
    assert leg.multipass is False
    assert leg.signal == "zero_seats"


def test_flight_missing_from_the_response_is_not_available() -> None:
    """Pulled, retimed, or gone -- either way we cannot fly it."""
    leg = parse_leg(
        response([{"bundle": "MULTIPASS"}], departure="2026-09-20T14:05:00"),
        "OTP", "EIN", "2026-09-20T06:50:00+03:00",
    )
    assert leg.found is False
    assert leg.multipass is False
    assert leg.signal == "flight_absent_from_search"


def test_no_fare_detail_is_unknown_not_a_guess() -> None:
    leg = parse_leg(
        response([]), "OTP", "EIN", "2026-09-20T06:50:00+03:00"
    )
    assert leg.found is True
    assert leg.multipass is None
    assert leg.signal == "no_fare_detail"


def test_matching_tolerates_a_bare_departure_date_field() -> None:
    body = {
        "outboundFlights": [
            {
                "departureStation": "OTP",
                "arrivalStation": "EIN",
                "departureDate": "2026-09-20T06:50:00",
                "fares": [{"bundle": "MULTIPASS"}],
            }
        ]
    }
    assert parse_leg(body, "OTP", "EIN", "2026-09-20T06:50:00+03:00").multipass is True


def test_garbage_response_is_an_error_not_a_crash() -> None:
    assert parse_leg("not json", "OTP", "EIN", "2026-09-20T06:50:00+03:00").error
    # No flight-list key at all: the response is broken, not empty.
    assert parse_leg({}, "OTP", "EIN", "2026-09-20T06:50:00+03:00").error
    assert parse_leg(
        {"outboundFlights": "nope"}, "OTP", "EIN", "2026-09-20T06:50:00+03:00"
    ).error


def test_an_empty_flight_list_is_a_real_answer_not_an_error() -> None:
    """"Nothing on sale" must stay distinct from "the API broke"."""
    leg = parse_leg(
        {"outboundFlights": []}, "OTP", "EIN", "2026-09-20T06:50:00+03:00"
    )
    assert leg.error is None
    assert leg.found is False
    assert leg.multipass is False
    assert leg.signal == "flight_absent_from_search"


def test_fares_given_as_a_mapping_are_handled() -> None:
    body = response({"a": {"bundle": "BASIC"}, "b": {"bundle": "MULTIPASS"}})
    assert parse_leg(body, "OTP", "EIN", "2026-09-20T06:50:00+03:00").multipass is True


# --- pacing -----------------------------------------------------------------


async def test_budget_spaces_out_checks() -> None:
    settings = WatcherSettings(max_checks_per_hour=10, min_seconds_between_checks=0)
    budget = CheckBudget(settings)
    assert budget.remaining == 10
    await budget.acquire()
    assert budget.remaining == 9


async def test_budget_blocks_once_the_hourly_cap_is_hit() -> None:
    settings = WatcherSettings(max_checks_per_hour=2, min_seconds_between_checks=0)
    budget = CheckBudget(settings)
    await budget.acquire()
    await budget.acquire()

    assert budget.remaining == 0
    assert budget.seconds_until_free() > 0
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(budget.acquire(), timeout=0.2)


async def test_budget_enforces_a_gap_between_checks() -> None:
    settings = WatcherSettings(max_checks_per_hour=100, min_seconds_between_checks=30)
    budget = CheckBudget(settings)
    await budget.acquire()
    assert budget.seconds_until_free() > 25


# --- recheck scheduling -----------------------------------------------------


def test_available_candidates_are_rechecked_quickly() -> None:
    settings = WatcherSettings(recheck_interval_min=20, hot_recheck_interval_min=3)
    now = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    closes = now + timedelta(hours=40)

    hot = next_check_time(now, closes, settings, AVAILABLE)
    cold = next_check_time(now, closes, settings, SOLD_OUT)
    assert hot == now + timedelta(minutes=3)
    assert cold == now + timedelta(minutes=20)


def test_checks_speed_up_as_the_window_closes() -> None:
    settings = WatcherSettings(
        recheck_interval_min=20, hot_recheck_interval_min=3, hot_window_min=30
    )
    now = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    closes = now + timedelta(minutes=25)
    assert next_check_time(now, closes, settings, SOLD_OUT) == now + timedelta(minutes=3)


def test_no_further_checks_once_the_window_would_have_closed() -> None:
    settings = WatcherSettings(recheck_interval_min=20, hot_recheck_interval_min=3)
    now = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    assert next_check_time(now, now + timedelta(minutes=2), settings, SOLD_OUT) is None
