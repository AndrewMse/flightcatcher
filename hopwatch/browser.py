"""Playwright session management.

Layer 3 needs a logged-in browser because Multipass eligibility is an
account-level fact: no unauthenticated endpoint will ever answer "can *your*
pass take this seat". The session lives in a persistent profile directory that
you seed once, by hand, with ``hopwatch login``.

Credentials are never handled by this program. The login command opens a real
browser and waits for *you* to sign in; nothing reads, stores, or types your
password.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Sequence

from . import selectors
from .settings import BrowserSettings

log = logging.getLogger(__name__)

try:  # Playwright is only needed for layers 3-4.
    from playwright.async_api import (
        BrowserContext,
        Page,
        Response,
        TimeoutError as PlaywrightTimeout,
        async_playwright,
    )

    PLAYWRIGHT_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only on partial installs
    BrowserContext = Page = Response = Any  # type: ignore[misc,assignment]
    PlaywrightTimeout = TimeoutError  # type: ignore[misc,assignment]
    async_playwright = None  # type: ignore[assignment]
    PLAYWRIGHT_AVAILABLE = False


class BrowserUnavailable(RuntimeError):
    pass


class NotLoggedIn(RuntimeError):
    pass


@dataclass
class CapturedResponse:
    url: str
    status: int
    body: Any
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class ResponseCollector:
    """Collects JSON responses the page fetches for itself.

    Reading the app's own API traffic is much more durable than scraping the
    rendered DOM, and it is the only way to see fare detail the page renders as
    icons rather than text.
    """

    def __init__(self, page: Page, paths: Sequence[str]) -> None:
        self.page = page
        self.paths = tuple(paths)
        self.captured: list[CapturedResponse] = []
        self._waiters: list[tuple[str, asyncio.Future]] = []
        page.on("response", self._on_response)

    def _on_response(self, response: Response) -> None:
        url = response.url
        if not any(p in url for p in self.paths):
            return
        asyncio.ensure_future(self._store(response))

    async def _store(self, response: Response) -> None:
        try:
            body = await response.json()
        except Exception:
            try:
                body = await response.text()
            except Exception:
                return
        entry = CapturedResponse(url=response.url, status=response.status, body=body)
        self.captured.append(entry)
        log.debug("captured %s %s", entry.status, entry.url)
        for path, future in list(self._waiters):
            if path in entry.url and not future.done():
                future.set_result(entry)

    def latest(self, path: str) -> CapturedResponse | None:
        matches = [c for c in self.captured if path in c.url]
        return matches[-1] if matches else None

    async def wait_for(self, path: str, timeout: float = 30.0) -> CapturedResponse:
        existing = self.latest(path)
        if existing:
            return existing
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiters.append((path, future))
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._waiters = [(p, f) for p, f in self._waiters if f is not future]


class BrowserSession:
    """A persistent, logged-in browser context."""

    def __init__(self, settings: BrowserSettings, headless: bool | None = None) -> None:
        if not PLAYWRIGHT_AVAILABLE:
            raise BrowserUnavailable(
                "Playwright is not installed. Run:\n"
                "  pip install 'hopwatch[browser]'\n"
                "  playwright install chromium"
            )
        self.settings = settings
        self.headless = settings.headless if headless is None else headless
        self._playwright = None
        self.context: BrowserContext | None = None

    async def start(self) -> BrowserSession:
        self.settings.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        self.context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.settings.profile_dir),
            headless=self.headless,
            slow_mo=self.settings.slow_mo_ms,
            locale=self.settings.locale,
            timezone_id=self.settings.timezone_id,
            viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        self.context.set_default_navigation_timeout(self.settings.nav_timeout_ms)
        self.context.set_default_timeout(self.settings.nav_timeout_ms)
        return self

    async def close(self) -> None:
        if self.context:
            await self.context.close()
            self.context = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

    async def __aenter__(self) -> BrowserSession:
        return await self.start()

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def new_page(self) -> Page:
        if not self.context:
            raise BrowserUnavailable("session not started")
        page = await self.context.new_page()
        return page

    # --- session state ------------------------------------------------------

    async def dismiss_cookie_banner(self, page: Page) -> None:
        """Decline non-essential cookies if a banner is in the way."""
        for selector in selectors.COOKIE_REJECT_BUTTONS:
            try:
                button = page.locator(selector).first
                if await button.count() and await button.is_visible():
                    await button.click(timeout=5_000)
                    log.debug("declined cookies via %s", selector)
                    return
            except Exception:
                continue

    async def is_logged_in(self, page: Page | None = None) -> bool:
        """Best-effort read of whether the stored profile still has a session.

        Deliberately conservative: an explicit logged-out marker wins over an
        ambiguous logged-in one, because acting while signed out wastes a
        booking window, while a false negative merely asks you to log in again.
        """
        owned = page is None
        page = page or await self.new_page()
        try:
            await page.goto(selectors.ACCOUNT_URL, wait_until="domcontentloaded")
            await self.dismiss_cookie_banner(page)
            await page.wait_for_timeout(2_500)

            for selector in selectors.LOGGED_OUT_MARKERS:
                try:
                    if await page.locator(selector).first.is_visible(timeout=1_000):
                        log.info("session looks logged out (%s)", selector)
                        return False
                except Exception:
                    continue

            for selector in selectors.LOGGED_IN_MARKERS:
                try:
                    if await page.locator(selector).first.is_visible(timeout=1_000):
                        return True
                except Exception:
                    continue

            log.warning(
                "could not classify session state from the page; "
                "run 'hopwatch probe --login' to recalibrate selectors"
            )
            return False
        finally:
            if owned:
                await page.close()

    async def require_login(self) -> None:
        if not await self.is_logged_in():
            raise NotLoggedIn(
                "No usable Wizz Air session. Run 'hopwatch login' and sign in."
            )

    # --- session durability -------------------------------------------------

    async def save_cookies(self) -> int:
        """Snapshot cookies outside the browser profile.

        Chromium only flushes its cookie store on a clean close, so a crash,
        an OOM kill, or `systemctl kill` loses the session even though nothing
        actually expired. This is cheap insurance against that.
        """
        if not self.context:
            return 0
        try:
            cookies = await self.context.cookies()
        except Exception as exc:  # noqa: BLE001
            log.debug("could not read cookies: %s", exc)
            return 0

        path = self.settings.cookie_backup
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(cookies))
            path.chmod(0o600)  # a live session is a credential
        except OSError as exc:
            log.warning("could not write cookie backup: %s", exc)
            return 0
        return len(cookies)

    async def restore_cookies(self) -> int:
        """Put a snapshot back, for when the profile lost its session."""
        if not self.context:
            return 0
        path = self.settings.cookie_backup
        if not path.exists():
            return 0
        try:
            cookies = json.loads(path.read_text())
            await self.context.add_cookies(cookies)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not restore cookie backup: %s", exc)
            return 0
        log.info("restored %d cookies from backup", len(cookies))
        return len(cookies)

    async def touch(self) -> bool:
        """Load an authenticated page to keep the session from idling out.

        The watcher can go hours between checks. Wizz expires idle sessions, so
        without this the session is reliably dead at the exact moment a booking
        window opens -- which is the only moment it matters.
        """
        page = await self.new_page()
        try:
            alive = await self.is_logged_in(page)
            if alive:
                await self.save_cookies()
            return alive
        finally:
            await page.close()


@asynccontextmanager
async def session(
    settings: BrowserSettings, headless: bool | None = None
) -> AsyncIterator[BrowserSession]:
    browser = BrowserSession(settings, headless=headless)
    await browser.start()
    try:
        yield browser
    finally:
        await browser.close()


# --- interactive commands ---------------------------------------------------


async def interactive_login(settings: BrowserSettings) -> bool:
    """Open a real browser and wait for the human to sign in.

    This program never sees, stores, or types your password. It opens the page,
    waits, and then checks whether a session now exists.
    """
    async with session(settings, headless=False) as browser:
        page = await browser.new_page()
        await page.goto(selectors.LOGIN_URL, wait_until="domcontentloaded")
        await browser.dismiss_cookie_banner(page)

        print("\nA browser window is open.")
        print("Sign in to Wizz Air yourself — including any 2FA.")
        print("Nothing here reads or stores your credentials.")
        print("\nWhen you are signed in and can see your account, press Enter.\n")
        await asyncio.get_running_loop().run_in_executor(None, input)

        ok = await browser.is_logged_in(page)
        if ok:
            saved = await browser.save_cookies()
            print(f"Session saved to {settings.profile_dir}")
            print(f"Backed up {saved} cookies to {settings.cookie_backup}")
        else:
            print(
                "Could not confirm a session. If you are definitely signed in, the "
                "detection markers in selectors.py may be stale — run "
                "'hopwatch probe --login' to see what the page serves."
            )
        return ok


async def probe(
    settings: BrowserSettings,
    origin: str | None = None,
    destination: str | None = None,
    departure: str | None = None,
    out_dir: Path | None = None,
) -> Path:
    """Dump what the live site actually serves, for calibrating selectors.

    Everything in this project that touches the logged-in site is written
    against assumptions that only reality can confirm. This makes reality
    inspectable instead of guessing.
    """
    out_dir = out_dir or Path.cwd() / "probe"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    async with session(settings, headless=False) as browser:
        page = await browser.new_page()
        collector = ResponseCollector(page, selectors.INTERESTING_API_PATHS)

        target = (
            selectors.flight_search_url(origin, destination, departure)
            if origin and destination and departure
            else selectors.ACCOUNT_URL
        )
        await page.goto(target, wait_until="domcontentloaded")
        await browser.dismiss_cookie_banner(page)
        await page.wait_for_timeout(12_000)

        report: dict[str, Any] = {
            "captured_at": stamp,
            "url": page.url,
            "logged_in_guess": await browser.is_logged_in(page),
            "marker_hits": {},
            "api_responses": [
                {"url": c.url, "status": c.status, "body": c.body}
                for c in collector.captured
            ],
        }

        groups = {
            "logged_in": selectors.LOGGED_IN_MARKERS,
            "logged_out": selectors.LOGGED_OUT_MARKERS,
            "payment": selectors.PAYMENT_FIELD_MARKERS,
            "flight_card": (selectors.FLIGHT_CARD,),
            "confirm": (selectors.CONFIRM_BUTTON,),
        }
        for name, group in groups.items():
            hits = {}
            for selector in group:
                try:
                    hits[selector] = await page.locator(selector).count()
                except Exception as exc:
                    hits[selector] = f"error: {exc}"
            report["marker_hits"][name] = hits

        shot = out_dir / f"probe-{stamp}.png"
        await page.screenshot(path=str(shot), full_page=True)
        html = out_dir / f"probe-{stamp}.html"
        html.write_text(await page.content())

        report_path = out_dir / f"probe-{stamp}.json"
        report_path.write_text(json.dumps(report, indent=2, default=str))

        print(f"\nProbe written to {out_dir}:")
        print(f"  {report_path.name}   API responses + selector hit counts")
        print(f"  {shot.name}   full-page screenshot")
        print(f"  {html.name}   page HTML")
        print(f"\nCaptured {len(collector.captured)} API response(s).")
        return report_path
