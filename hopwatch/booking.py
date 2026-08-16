"""Prefill-to-confirm booking.

The flow drives a booking as far as the final confirm button, screenshots
exactly what is about to be bought, and then stops. A human approves from
Discord or the web UI, and only then is confirm clicked.

Two rules are absolute here:

1. **No payment credentials, ever.** If the flow reaches anything that wants a
   card number, CVC, or a payment iframe, automation aborts and hands the
   session back to you. Multipass bookings should not ask -- if one does, that
   is exactly the case where a human should be looking at the screen.

2. **No confirm without a fresh, explicit approval.** Approval is per booking
   and expires; a stale one is never reused.

A multi-leg itinerary is several separate bookings. All legs are prepared
before any is confirmed, so approval covers the whole trip rather than
committing you to leg 1 with leg 2 still unknown.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from . import selectors
from .browser import BrowserSession
from .settings import BrowserSettings, PassengerSettings

log = logging.getLogger(__name__)
UTC = timezone.utc


class BookingAborted(RuntimeError):
    """Automation stopped deliberately; the session is left for a human."""


class PaymentWallReached(BookingAborted):
    """The flow asked for payment credentials. That is a hard stop."""


class StepNotFound(BookingAborted):
    """A step of the flow could not be located on the page."""


@dataclass
class PreparedLeg:
    origin: str
    destination: str
    departs_local: str
    page: Any = None
    screenshot: Path | None = None
    price_text: str = ""
    steps: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.origin}→{self.destination} {self.departs_local[:16].replace('T', ' ')}"


@dataclass
class PreparedBooking:
    legs: list[PreparedLeg] = field(default_factory=list)
    prepared_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    notes: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return " + ".join(leg.label for leg in self.legs)

    def to_detail(self) -> dict[str, Any]:
        return {
            "prepared_at": self.prepared_at.isoformat(),
            "notes": self.notes,
            "legs": [
                {
                    "origin": leg.origin,
                    "destination": leg.destination,
                    "departs_local": leg.departs_local,
                    "price_text": leg.price_text,
                    "screenshot": str(leg.screenshot) if leg.screenshot else None,
                    "steps": leg.steps,
                }
                for leg in self.legs
            ],
        }


class BookingFlow:
    def __init__(
        self,
        browser: BrowserSession,
        browser_settings: BrowserSettings,
        passenger: PassengerSettings,
    ) -> None:
        self.browser = browser
        self.browser_settings = browser_settings
        self.passenger = passenger

    # --- safety -------------------------------------------------------------

    async def _assert_no_payment_fields(self, page: Any, where: str) -> None:
        """Abort if the page is asking for card details.

        Deliberately checks for *presence*, not just visibility: a payment
        iframe that has not painted yet is still a payment iframe.
        """
        for marker in selectors.PAYMENT_FIELD_MARKERS:
            try:
                if await page.locator(marker).count():
                    raise PaymentWallReached(
                        f"payment field ({marker}) present at {where}. "
                        "Automation stopped — finish this booking yourself."
                    )
            except PaymentWallReached:
                raise
            except Exception:
                continue

    async def _screenshot(self, page: Any, name: str) -> Path:
        directory = self.browser_settings.screenshot_dir
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        path = directory / f"{stamp}-{name}.png"
        await page.screenshot(path=str(path), full_page=True)
        return path

    @staticmethod
    async def _click_first(page: Any, selector: str, what: str, timeout: int = 15_000) -> None:
        locator = page.locator(selector).first
        try:
            await locator.wait_for(state="visible", timeout=timeout)
            await locator.click()
        except Exception as exc:
            raise StepNotFound(
                f"could not {what} (selector {selector!r}): {exc}. "
                "Run 'hopwatch probe' and update selectors.py."
            ) from exc

    @staticmethod
    async def _fill_if_present(page: Any, selector: str, value: str) -> bool:
        if not value:
            return False
        try:
            locator = page.locator(selector).first
            if await locator.count() and await locator.is_visible():
                await locator.fill(value)
                return True
        except Exception:
            pass
        return False

    # --- preparation --------------------------------------------------------

    async def prepare_leg(self, leg: dict[str, Any]) -> PreparedLeg:
        """Drive one leg to the confirm step and stop there."""
        prepared = PreparedLeg(
            origin=leg["origin"],
            destination=leg["destination"],
            departs_local=leg["departs_local"],
        )
        page = await self.browser.new_page()
        prepared.page = page
        try:
            url = selectors.flight_search_url(
                leg["origin"], leg["destination"], leg["departs_local"][:10]
            )
            await page.goto(url, wait_until="domcontentloaded")
            await self.browser.dismiss_cookie_banner(page)
            prepared.steps.append("opened flight select")
            await self._assert_no_payment_fields(page, "flight select")

            await self._click_first(
                page, selectors.MULTIPASS_FARE_BUTTON, "select the Multipass fare"
            )
            prepared.steps.append("selected Multipass fare")

            await self._click_first(
                page, selectors.CONTINUE_BUTTON, "continue from flight select"
            )
            prepared.steps.append("continued to passenger details")
            await page.wait_for_timeout(2_500)
            await self._assert_no_payment_fields(page, "passenger details")

            filled = []
            if await self._fill_if_present(
                page, selectors.PASSENGER_FIRST_NAME, self.passenger.first_name
            ):
                filled.append("first name")
            if await self._fill_if_present(
                page, selectors.PASSENGER_LAST_NAME, self.passenger.last_name
            ):
                filled.append("last name")
            if await self._fill_if_present(
                page, selectors.PASSENGER_DOB, self.passenger.date_of_birth
            ):
                filled.append("date of birth")
            if await self._fill_if_present(
                page, selectors.CONTACT_EMAIL, self.passenger.email
            ):
                filled.append("email")
            if await self._fill_if_present(
                page, selectors.CONTACT_PHONE, self.passenger.phone
            ):
                filled.append("phone")
            prepared.steps.append(f"filled: {', '.join(filled) or 'nothing (prefilled?)'}")

            await self._click_first(
                page, selectors.CONTINUE_BUTTON, "continue from passenger details"
            )
            await page.wait_for_timeout(3_000)
            prepared.steps.append("reached confirm step")

            # The important check: we are about to sit on a page with a confirm
            # button. If that page also wants a card, we do not belong here.
            await self._assert_no_payment_fields(page, "confirm step")

            confirm = page.locator(selectors.CONFIRM_BUTTON).first
            if not await confirm.count():
                raise StepNotFound(
                    "no confirm button on the final page — flow may have changed. "
                    "Run 'hopwatch probe' and update selectors.py."
                )

            try:
                prepared.price_text = (await page.locator("body").inner_text())[:400]
            except Exception:
                prepared.price_text = ""

            prepared.screenshot = await self._screenshot(
                page, f"confirm-{leg['origin']}-{leg['destination']}"
            )
            return prepared
        except Exception:
            await self.abandon_leg(prepared)
            raise

    async def prepare(self, legs: Sequence[dict[str, Any]]) -> PreparedBooking:
        """Prepare every leg before committing to any of them."""
        booking = PreparedBooking()
        try:
            for leg in legs:
                booking.legs.append(await self.prepare_leg(leg))
            if len(legs) > 1:
                booking.notes.append(
                    f"{len(legs)} separate bookings will be confirmed back to back; "
                    "a failure partway leaves you holding only the earlier legs"
                )
            return booking
        except Exception:
            await self.abandon(booking)
            raise

    # --- commitment ---------------------------------------------------------

    async def confirm(self, booking: PreparedBooking) -> dict[str, Any]:
        """Click confirm on every prepared leg. Only ever called after approval.

        Legs are confirmed in departure order and the result records exactly
        how far it got, because a partial success is the stranded-in-Warsaw
        case and must never be reported as a plain failure.
        """
        confirmed: list[dict[str, Any]] = []
        for leg in booking.legs:
            try:
                await self._assert_no_payment_fields(leg.page, f"confirm of {leg.label}")
                await self._click_first(
                    leg.page, selectors.CONFIRM_BUTTON, f"confirm {leg.label}", timeout=20_000
                )
                await leg.page.wait_for_timeout(6_000)

                reference = None
                try:
                    node = leg.page.locator(selectors.BOOKING_REFERENCE).first
                    if await node.count():
                        reference = (await node.inner_text()).strip()
                except Exception:
                    pass

                shot = await self._screenshot(leg.page, f"booked-{leg.origin}-{leg.destination}")
                confirmed.append(
                    {
                        "leg": leg.label,
                        "reference": reference,
                        "screenshot": str(shot),
                    }
                )
            except Exception as exc:
                return {
                    "ok": False,
                    "confirmed": confirmed,
                    "failed_leg": leg.label,
                    "error": str(exc),
                    "partial": bool(confirmed),
                }
            finally:
                pass

        await self.abandon(booking)
        return {"ok": True, "confirmed": confirmed, "partial": False}

    # --- cleanup ------------------------------------------------------------

    async def abandon_leg(self, leg: PreparedLeg) -> None:
        if leg.page is not None:
            try:
                await leg.page.close()
            except Exception:
                pass
            leg.page = None

    async def abandon(self, booking: PreparedBooking) -> None:
        await asyncio.gather(
            *(self.abandon_leg(leg) for leg in booking.legs), return_exceptions=True
        )
