"""Static configuration for FlightCatcher.

Layers 1-2 of the bot (route graph + schedule feasibility) only ever touch
Wizz Air's unauthenticated endpoints. Nothing in here needs credentials.
"""

from __future__ import annotations

import os
from pathlib import Path

# --- Wizz Air backend -------------------------------------------------------

HOMEPAGE = "https://www.wizzair.com/en-gb"
BACKEND = "https://be.wizzair.com"

# The backend embeds a version in the URL path and rotates it every few weeks,
# so the client scrapes the live one from the homepage. This is only the
# fallback for when that scrape fails.
FALLBACK_API_VERSION = "29.16.1"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# --- Politeness -------------------------------------------------------------
# These endpoints are open but not ours. Stay well under anything that could
# look like a scrape: the account attached to Multipass is what's at stake.

MIN_REQUEST_INTERVAL = 1.5  # seconds, enforced globally across threads
MAX_CONCURRENCY = 3
MAX_RETRIES = 4
BACKOFF_BASE = 2.0
REQUEST_TIMEOUT = 30.0

# --- Cache ------------------------------------------------------------------

CACHE_DIR = Path(
    os.environ.get("FLIGHTCATCHER_CACHE", Path.home() / ".cache" / "flightcatcher")
)
VERSION_TTL = 6 * 3600
MAP_TTL = 24 * 3600
TIMETABLE_TTL = 15 * 60

# --- Multipass --------------------------------------------------------------

# Booking opens exactly this long before departure. This constant is the whole
# reason the bot exists, and drives all the window math in search.py.
MULTIPASS_WINDOW_HOURS = 72
# ...and closes this long before departure (check-in cutoff).
MULTIPASS_CUTOFF_HOURS = 3
MULTIPASS_FARE_EUR = 10.0

# --- Flight duration model --------------------------------------------------
# The timetable endpoint gives departure times but no arrival times, so block
# time is estimated from great-circle distance. Calibrated against a handful of
# published Wizz schedules; accurate to roughly +/-15 min on European sectors,
# which a 3h minimum layover absorbs comfortably.

CRUISE_KMH = 780.0
ROUTE_INEFFICIENCY = 1.05  # airways are not great circles
FIXED_OVERHEAD_MIN = 30.0  # taxi, climb, descent, approach

# --- Search defaults --------------------------------------------------------

DEFAULT_MIN_LAYOVER_MIN = 180
DEFAULT_MAX_LAYOVER_MIN = 20 * 60
DEFAULT_MAX_STOPS = 1
DEFAULT_MAX_DETOUR = 2.2  # path distance / direct distance
DEFAULT_MAX_TRIP_HOURS = 30.0

# A self-transfer between two airports in the same metro area (OTP -> BBU)
# means a taxi with bags, so it needs a lot more slack than a terminal walk.
GROUND_TRANSFER_EXTRA_MIN = 120

# The timetable endpoint silently truncates long spans; chunk requests below it.
TIMETABLE_MAX_SPAN_DAYS = 40

# How many days past the requested departure window later legs may depart.
CONNECTION_LOOKAHEAD_DAYS = 2
