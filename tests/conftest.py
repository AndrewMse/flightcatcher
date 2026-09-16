"""Synthetic route map fixtures.

Shaped like the real ``asset/map`` payload -- including the awkward bits: metro
codes, MAC pseudo-stations that are groupings rather than airports, and station
names with trailing CRLFs.
"""

from __future__ import annotations

import pytest

from hopwatch.network import RouteNetwork

from .contract.conftest import aws  # noqa: F401  (moto-mocked AWS for any test)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path_factory, monkeypatch):
    """Keep every test away from the real ~/.config and ~/.local/share.

    Settings.load migrates FlightCatcher-era files it finds next to the
    default paths. Pointed at a real home directory, a test run would move
    the developer's own database and browser profile.
    """
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    for name in ("HOPWATCH_CONFIG", "HOPWATCH_DB", "FLIGHTCATCHER_CONFIG", "FLIGHTCATCHER_DB"):
        monkeypatch.delenv(name, raising=False)
    return home

STATIONS = {
    # iata: (name, country_code, country_name, lat, lon, mac)
    "OTP": ("Bucharest Otopeni\r\n", "RO", "Romania", 44.5711, 26.0850, "BUH"),
    "BBU": ("Bucharest Baneasa", "RO", "Romania", 44.5032, 26.1021, "BUH"),
    "WAW": ("Warsaw Chopin\r\n", "PL", "Poland", 52.1657, 20.9671, "WSW"),
    "WMI": ("Warsaw Modlin", "PL", "Poland", 52.4511, 20.6518, "WSW"),
    "WSW": ("Warsaw", "PL", "Poland", 52.2297, 21.0122, "WSW"),  # pseudo-station
    "EIN": ("Eindhoven", "NL", "Netherlands", 51.4501, 5.3745, None),
    "BUD": ("Budapest", "HU", "Hungary", 47.4369, 19.2556, None),
    "EVN": ("Yerevan", "AM", "Armenia", 40.1473, 44.3959, None),
    "LPA": ("Gran Canaria", "ES", "Spain", 27.9319, -15.3866, None),
    "MAD": ("Madrid", "ES", "Spain", 40.4719, -3.5626, None),
}

# Deliberately includes WSW (a MAC) as a connection target, as the real feed does.
ROUTES = {
    "OTP": ["WSW", "BUD", "EVN", "EIN", "LPA"],
    "BBU": ["WSW", "BUD", "EIN"],
    "WAW": ["EIN", "OTP"],
    "WMI": ["BUD"],
    "BUD": ["EIN", "OTP"],
    "EVN": ["EIN"],
    "EIN": ["OTP"],
    "LPA": ["MAD"],
    "MAD": ["EIN"],
}


def build_map(routes: dict[str, list[str]] | None = None) -> dict:
    routes = routes if routes is not None else ROUTES
    cities = []
    for iata, (name, cc, cn, lat, lon, mac) in STATIONS.items():
        cities.append(
            {
                "iata": iata,
                "shortName": name,
                "countryCode": cc,
                "countryName": cn,
                "latitude": lat,
                "longitude": lon,
                "mac": mac,
                "connections": [
                    {"iata": dest, "operationStartDate": None, "isDirectFlight": True}
                    for dest in routes.get(iata, [])
                ],
            }
        )
    return {"cities": cities}


@pytest.fixture
def net() -> RouteNetwork:
    return RouteNetwork(build_map())
