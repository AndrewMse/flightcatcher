from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from flightcatcher import config
from flightcatcher.models import Departure, Itinerary
from flightcatcher.network import RouteNetwork
from flightcatcher.search import (
    SearchOptions,
    _connection_between,
    _to_departures,
    booking_window,
    build_itineraries,
)
from flightcatcher.timezones import tz_for

UTC = timezone.utc


def dep(net: RouteNetwork, origin: str, dest: str, local: str) -> Departure:
    airport = net.airport(origin)
    zone = tz_for(origin, airport.country_code)
    naive = datetime.fromisoformat(local)
    aware = naive.replace(tzinfo=zone)
    return Departure(
        origin=origin,
        destination=dest,
        departs_local=aware,
        departs_utc=aware.astimezone(UTC),
        duration_min=net.duration_min(origin, dest),
        price_amount=None,
        price_currency=None,
    )


def opts(**kwargs) -> SearchOptions:
    base = dict(date_from=date(2026, 9, 15), date_to=date(2026, 9, 22))
    base.update(kwargs)
    return SearchOptions(**base)


# --- timezone-correct connection maths --------------------------------------


def test_layover_is_computed_in_utc_not_local_time(net: RouteNetwork) -> None:
    """Bucharest is UTC+3 and Budapest UTC+2, so local-time subtraction lies."""
    leg1 = dep(net, "OTP", "BUD", "2026-09-15T22:55")
    leg2 = dep(net, "BUD", "EIN", "2026-09-16T06:15")

    connection = _connection_between(net, leg1, leg2)
    assert connection is not None

    expected = int(
        (leg2.departs_utc - leg1.arrives_utc).total_seconds() // 60
    )
    assert connection.layover_min == expected

    # The naive answer -- subtracting wall-clock times -- is an hour out.
    naive = int(
        (
            leg2.departs_local.replace(tzinfo=None)
            - (leg1.departs_local.replace(tzinfo=None)
               + timedelta(minutes=leg1.duration_min))
        ).total_seconds()
        // 60
    )
    assert connection.layover_min != naive
    assert connection.layover_min == naive + 60


def test_connection_requires_the_same_metro_area(net: RouteNetwork) -> None:
    leg1 = dep(net, "OTP", "WAW", "2026-09-15T08:00")
    unrelated = dep(net, "BUD", "EIN", "2026-09-15T18:00")
    assert _connection_between(net, leg1, unrelated) is None

    onward = dep(net, "WAW", "EIN", "2026-09-15T18:00")
    assert _connection_between(net, leg1, onward) is not None


def test_ground_transfer_is_flagged_and_gated(net: RouteNetwork) -> None:
    """Landing at WAW and departing WMI means a bus across Warsaw."""
    leg1 = dep(net, "OTP", "WAW", "2026-09-15T08:00")
    leg2 = dep(net, "WMI", "BUD", "2026-09-15T20:00")

    connection = _connection_between(net, leg1, leg2)
    assert connection is not None
    assert connection.ground_transfer is True
    assert (connection.from_station, connection.to_station) == ("WAW", "WMI")


def test_ground_transfer_needs_a_bigger_buffer(net: RouteNetwork) -> None:
    from flightcatcher.search import _layover_ok
    from flightcatcher.models import Connection

    tight = Connection(layover_min=200, ground_transfer=True, from_station="WAW",
                       to_station="WMI")
    roomy = Connection(layover_min=200 + config.GROUND_TRANSFER_EXTRA_MIN,
                       ground_transfer=True, from_station="WAW", to_station="WMI")
    options = opts(min_layover_min=180, allow_ground_transfer=True)

    assert not _layover_ok(tight, options)
    assert _layover_ok(roomy, options)
    assert not _layover_ok(roomy, opts(min_layover_min=180))  # not allowed at all


# --- Multipass booking window -----------------------------------------------


def test_direct_flight_window_opens_72h_before(net: RouteNetwork) -> None:
    leg = dep(net, "OTP", "EIN", "2026-09-20T06:50")
    itinerary = Itinerary(legs=[leg])
    window = booking_window(itinerary, now=datetime(2026, 9, 15, tzinfo=UTC))

    assert window.opens_utc == leg.departs_utc - timedelta(hours=72)
    assert window.closes_utc == leg.departs_utc - timedelta(hours=3)
    assert window.staggered_hours == 0.0
    assert window.status == "too_early"


def test_connecting_itinerary_opens_with_its_last_leg(net: RouteNetwork) -> None:
    """The whole trip is only safe to buy once every leg has unlocked."""
    leg1 = dep(net, "OTP", "BUD", "2026-09-20T22:55")
    leg2 = dep(net, "BUD", "EIN", "2026-09-21T06:15")
    itinerary = Itinerary(legs=[leg1, leg2])

    window = booking_window(itinerary, now=datetime(2026, 9, 15, tzinfo=UTC))

    assert window.opens_utc == leg2.departs_utc - timedelta(hours=72)
    assert window.opens_utc > window.first_leg_opens_utc
    assert window.closes_utc == leg1.departs_utc - timedelta(hours=3)
    # 19:55Z to 04:15Z: over eight hours where leg 1 is buyable and leg 2 is not.
    assert window.staggered_hours == 8.3


def test_window_status_transitions(net: RouteNetwork) -> None:
    leg = dep(net, "OTP", "EIN", "2026-09-20T06:50")
    itinerary = Itinerary(legs=[leg])
    opens = leg.departs_utc - timedelta(hours=72)
    closes = leg.departs_utc - timedelta(hours=3)

    assert booking_window(itinerary, opens - timedelta(minutes=1)).status == "too_early"
    assert booking_window(itinerary, opens).status == "open"
    assert booking_window(itinerary, closes - timedelta(minutes=1)).status == "open"
    assert booking_window(itinerary, closes).status == "closed"


# --- itinerary assembly -----------------------------------------------------


def test_build_itineraries_applies_layover_bounds(net: RouteNetwork) -> None:
    path = ["OTP", "BUD", "EIN"]
    departures = {
        ("OTP", "BUD"): [dep(net, "OTP", "BUD", "2026-09-15T08:00")],
        ("BUD", "EIN"): [
            dep(net, "BUD", "EIN", "2026-09-15T08:30"),  # impossibly tight
            dep(net, "BUD", "EIN", "2026-09-15T14:00"),  # comfortable
            dep(net, "BUD", "EIN", "2026-09-17T14:00"),  # far too late
        ],
    }

    found = build_itineraries(net, path, departures, opts(min_layover_min=180))
    assert len(found) == 1
    assert found[0].legs[1].departs_local.hour == 14
    assert found[0].stops == 1
    assert found[0].path == path


def test_build_itineraries_honours_max_trip_hours(net: RouteNetwork) -> None:
    path = ["OTP", "BUD", "EIN"]
    departures = {
        ("OTP", "BUD"): [dep(net, "OTP", "BUD", "2026-09-15T08:00")],
        ("BUD", "EIN"): [dep(net, "BUD", "EIN", "2026-09-16T14:00")],
    }
    assert build_itineraries(net, path, departures, opts(max_layover_min=3000,
                                                        max_trip_hours=40))
    assert not build_itineraries(net, path, departures, opts(max_layover_min=3000,
                                                             max_trip_hours=12))


def test_first_leg_must_fall_inside_the_requested_dates(net: RouteNetwork) -> None:
    path = ["OTP", "EIN"]
    departures = {
        ("OTP", "EIN"): [
            dep(net, "OTP", "EIN", "2026-09-14T08:00"),  # before window
            dep(net, "OTP", "EIN", "2026-09-16T08:00"),  # inside
            dep(net, "OTP", "EIN", "2026-09-30T08:00"),  # after window
        ]
    }
    found = build_itineraries(net, path, departures, opts())
    assert [i.legs[0].departs_local.day for i in found] == [16]


def test_departure_hour_filters(net: RouteNetwork) -> None:
    path = ["OTP", "EIN"]
    departures = {
        ("OTP", "EIN"): [
            dep(net, "OTP", "EIN", "2026-09-16T05:00"),
            dep(net, "OTP", "EIN", "2026-09-16T12:00"),
            dep(net, "OTP", "EIN", "2026-09-16T22:00"),
        ]
    }
    found = build_itineraries(
        net, path, departures, opts(earliest_departure_hour=8, latest_departure_hour=18)
    )
    assert [i.legs[0].departs_local.hour for i in found] == [12]


def test_missing_timetable_for_any_leg_yields_nothing(net: RouteNetwork) -> None:
    departures = {("OTP", "BUD"): [dep(net, "OTP", "BUD", "2026-09-15T08:00")]}
    assert build_itineraries(net, ["OTP", "BUD", "EIN"], departures, opts()) == []


# --- timetable parsing ------------------------------------------------------


def test_to_departures_trusts_the_station_the_api_reports(net: RouteNetwork) -> None:
    """A query for OTP can come back as BBU; the response wins."""
    flights = [
        {
            "departureStation": "BBU",
            "arrivalStation": "EIN",
            "departureDates": ["2026-09-16T22:05:00"],
            "price": {"amount": 0.0, "currencyCode": "RON"},
            "originalPrice": {"amount": 774.0, "currencyCode": "RON"},
            "priceType": "checkPrice",
        }
    ]
    parsed = _to_departures(net, flights, ("OTP", "EIN"))
    assert len(parsed) == 1
    assert parsed[0].origin == "BBU"
    # checkPrice means "amount: 0" is a placeholder, not a free seat.
    assert parsed[0].price_amount == 774.0


def test_to_departures_converts_local_times_to_utc(net: RouteNetwork) -> None:
    flights = [
        {
            "departureStation": "OTP",
            "arrivalStation": "EIN",
            "departureDates": ["2026-09-16T06:50:00"],
            "price": {"amount": 100.0, "currencyCode": "RON"},
            "priceType": "price",
        }
    ]
    parsed = _to_departures(net, flights, ("OTP", "EIN"))
    # Bucharest is UTC+3 in September.
    assert parsed[0].departs_utc == datetime(2026, 9, 16, 3, 50, tzinfo=UTC)


def test_to_departures_dedupes_and_sorts(net: RouteNetwork) -> None:
    flights = [
        {
            "departureStation": "OTP",
            "arrivalStation": "EIN",
            "departureDates": ["2026-09-17T06:50:00", "2026-09-16T06:50:00"],
            "priceType": "price",
        },
        {
            "departureStation": "OTP",
            "arrivalStation": "EIN",
            "departureDates": ["2026-09-16T06:50:00"],
            "priceType": "price",
        },
    ]
    parsed = _to_departures(net, flights, ("OTP", "EIN"))
    assert len(parsed) == 2
    assert parsed[0].departs_utc < parsed[1].departs_utc
