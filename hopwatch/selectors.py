"""Every site-specific string, in one place.

Wizz Air redesigns without warning, so all knowledge of *their* DOM is
quarantined here rather than scattered through the automation code. When
something breaks, this is the only file that should need editing.

The strategy everywhere else is to lean on intercepted API responses rather
than the DOM, precisely because these selectors are the fragile part. They are
used only where a click genuinely has to happen.

Run ``hopwatch probe`` to dump what the live site actually serves, then
correct anything here that has drifted.
"""

from __future__ import annotations

# --- URLs -------------------------------------------------------------------

BASE = "https://www.wizzair.com/en-gb"
LOGIN_URL = f"{BASE}/login"
ACCOUNT_URL = f"{BASE}/#/profile"
MULTIPASS_URL = f"{BASE}/#/multipass"
MULTIPASS_BOOKING_URL = f"{BASE}/#/booking/select-flight"


def flight_search_url(origin: str, destination: str, departure: str) -> str:
    """Deep link into the flight-select step for one date.

    ``departure`` is ISO ``YYYY-MM-DD``.
    """
    return (
        f"{BASE}/#/booking/select-flight/{origin}/{destination}/{departure}/null/1/0/0/null"
    )


# --- API paths worth intercepting -------------------------------------------
# These are what the page itself calls. Reading its responses is far more
# durable than scraping the rendered result.

API_SEARCH = "/Api/search/search"
API_MULTIPASS = "/Api/multipass"
API_CUSTOMER = "/Api/customer"
API_BOOKING = "/Api/booking"

INTERESTING_API_PATHS = (API_SEARCH, API_MULTIPASS, API_CUSTOMER, API_BOOKING)


# --- Login / session --------------------------------------------------------

# Presence of any of these means "not signed in".
LOGGED_OUT_MARKERS = (
    "input[type='password']",
    "[data-test='login-submit']",
    "button[data-test='header-login']",
)

# Presence of any of these means "signed in".
LOGGED_IN_MARKERS = (
    "[data-test='header-profile']",
    "[data-test='profile-menu']",
    "a[href*='profile']",
)


# --- Cookie / consent banners -----------------------------------------------
# Answered with the most privacy-preserving option available.

COOKIE_REJECT_BUTTONS = (
    "#onetrust-reject-all-handler",
    "button[data-test='cookie-reject-all']",
    "button:has-text('Reject all')",
)
COOKIE_BANNER = "#onetrust-banner-sdk, [data-test='cookie-banner']"


# --- Flight selection -------------------------------------------------------

FLIGHT_CARD = "[data-test='flight-select-card'], .flight-select__card"
MULTIPASS_FARE_BUTTON = (
    "[data-test='fare-multipass'], button:has-text('Multipass')"
)
CONTINUE_BUTTON = (
    "[data-test='flight-select-continue'], button:has-text('Continue')"
)


# --- Passenger details ------------------------------------------------------

PASSENGER_FIRST_NAME = "input[name*='firstName'], [data-test='passenger-first-name']"
PASSENGER_LAST_NAME = "input[name*='lastName'], [data-test='passenger-last-name']"
PASSENGER_DOB = "input[name*='dateOfBirth'], [data-test='passenger-dob']"
PASSENGER_GENDER = "[data-test='passenger-gender']"
CONTACT_EMAIL = "input[type='email'], [data-test='contact-email']"
CONTACT_PHONE = "input[type='tel'], [data-test='contact-phone']"


# --- The final confirm step -------------------------------------------------

CONFIRM_BUTTON = (
    "[data-test='payment-submit'], [data-test='booking-confirm'], "
    "button:has-text('Confirm booking')"
)
BOOKING_REFERENCE = "[data-test='booking-reference'], .booking-reference"


# --- Payment fields: the tripwire -------------------------------------------
# If any of these is present and required, automation stops dead. The bot does
# not type payment credentials under any circumstance, so a flow that asks for
# them is a flow the human has to finish. See booking.py.

PAYMENT_FIELD_MARKERS = (
    "input[name*='cardNumber']",
    "input[name*='card-number']",
    "input[autocomplete='cc-number']",
    "input[name*='cvc']",
    "input[name*='cvv']",
    "input[autocomplete='cc-csc']",
    "iframe[name*='card']",
    "iframe[src*='payment']",
    "iframe[title*='card' i]",
)
