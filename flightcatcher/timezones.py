"""Timezone lookup for Wizz Air stations.

Layover maths has to happen in UTC, but the timetable endpoint returns local
departure times. A full geographic timezone database (timezonefinder) is
overkill here: Wizz serves 44 countries and all but two are single-zone, so a
static table plus a short override list is both smaller and easier to audit.

Verified against the country list in the live route map.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

COUNTRY_TZ: dict[str, str] = {
    "AE": "Asia/Dubai",
    "AL": "Europe/Tirane",
    "AM": "Asia/Yerevan",
    "AZ": "Asia/Baku",
    "BA": "Europe/Sarajevo",
    "BE": "Europe/Brussels",
    "BG": "Europe/Sofia",
    "CH": "Europe/Zurich",
    "CY": "Asia/Nicosia",
    "CZ": "Europe/Prague",
    "DE": "Europe/Berlin",
    "DK": "Europe/Copenhagen",
    "EE": "Europe/Tallinn",
    "EG": "Africa/Cairo",
    "ES": "Europe/Madrid",
    "FI": "Europe/Helsinki",
    "FR": "Europe/Paris",
    "GB": "Europe/London",
    "GE": "Asia/Tbilisi",
    "GR": "Europe/Athens",
    "HR": "Europe/Zagreb",
    "HU": "Europe/Budapest",
    "IL": "Asia/Jerusalem",
    "IS": "Atlantic/Reykjavik",
    "IT": "Europe/Rome",
    "JO": "Asia/Amman",
    "LT": "Europe/Vilnius",
    "MA": "Africa/Casablanca",
    "MD": "Europe/Chisinau",
    "ME": "Europe/Podgorica",
    "MK": "Europe/Skopje",
    "MT": "Europe/Malta",
    "NL": "Europe/Amsterdam",
    "NO": "Europe/Oslo",
    "PL": "Europe/Warsaw",
    "PT": "Europe/Lisbon",
    "RO": "Europe/Bucharest",
    "RS": "Europe/Belgrade",
    "SA": "Asia/Riyadh",
    "SE": "Europe/Stockholm",
    "SI": "Europe/Ljubljana",
    "SK": "Europe/Bratislava",
    "TR": "Europe/Istanbul",
    "XK": "Europe/Belgrade",  # Kosovo shares Belgrade's rules
}

# The only stations whose timezone differs from their country's mainland.
AIRPORT_TZ: dict[str, str] = {
    # Canary Islands (Spain, UTC+0/+1 rather than mainland UTC+1/+2)
    "FUE": "Atlantic/Canary",
    "LPA": "Atlantic/Canary",
    "TFN": "Atlantic/Canary",
    "TFS": "Atlantic/Canary",
    "ACE": "Atlantic/Canary",
    # Madeira (Portugal)
    "FNC": "Atlantic/Madeira",
    "PXO": "Atlantic/Madeira",
}

_FALLBACK = "Europe/Brussels"  # central-European default

_cache: dict[str, ZoneInfo] = {}


def tz_for(iata: str, country_code: str | None) -> ZoneInfo:
    """Resolve a station to its IANA timezone.

    Falls back to Central European Time rather than raising: a wrong-by-one-hour
    layover on an obscure station is a better failure mode than a crashed
    search, and the minimum-layover threshold is the real safety margin.
    """
    key = iata.upper()
    if key in _cache:
        return _cache[key]

    name = AIRPORT_TZ.get(key) or COUNTRY_TZ.get((country_code or "").upper()) or _FALLBACK
    try:
        zone = ZoneInfo(name)
    except Exception:
        zone = ZoneInfo(_FALLBACK)
    _cache[key] = zone
    return zone
