"""Client for Wizz Air's unauthenticated backend endpoints.

Two endpoints are open to plain HTTP and carry everything layers 1-2 need:

    GET  /{version}/Api/asset/map        the full route network
    POST /{version}/Api/search/timetable which dates a route flies, and at what time

A third, ``/Api/search/search``, holds actual seat inventory but sits behind a
WAF and answers 429 to anything without a real browser session. That one is
layer 3's problem and deliberately not implemented here.

``{version}`` rotates every few weeks, so it is scraped from the homepage and
cached rather than hardcoded.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx

from . import config

log = logging.getLogger(__name__)

_VERSION_RE = re.compile(r"be\.wizzair\.com/(\d+\.\d+\.\d+)")


class WizzError(RuntimeError):
    pass


class _RateLimiter:
    """Global minimum spacing between backend calls, safe across threads."""

    def __init__(self, min_interval: float) -> None:
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            delta = time.monotonic() - self._last
            if delta < self._min_interval:
                time.sleep(self._min_interval - delta)
            self._last = time.monotonic()


class _DiskCache:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", key)
        return self.root / f"{safe}.json"

    def get(self, key: str, ttl: float) -> Any | None:
        path = self._path(key)
        try:
            if time.time() - path.stat().st_mtime > ttl:
                return None
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None

    def set(self, key: str, value: Any) -> None:
        try:
            self._path(key).write_text(json.dumps(value))
        except OSError as exc:  # a broken cache must never break a search
            log.warning("cache write failed for %s: %s", key, exc)

    def get_stale(self, key: str) -> Any | None:
        """Last known value regardless of age, for use when the network fails."""
        return self.get(key, ttl=float("inf"))


class WizzClient:
    def __init__(self, cache_dir: Path | None = None, offline: bool = False) -> None:
        self.cache = _DiskCache(cache_dir or config.CACHE_DIR)
        self.offline = offline
        self._limiter = _RateLimiter(config.MIN_REQUEST_INTERVAL)
        self._version: str | None = None
        self._version_lock = threading.Lock()
        # The backend rotates the anti-forgery token on every response, so
        # reading the cookie and sending the request have to be atomic. Without
        # this, concurrent workers race: one sends a token another has already
        # invalidated, and every second call 400s and retries. Costs nothing in
        # throughput -- the rate limiter is what actually paces us.
        self._request_lock = threading.Lock()
        self._http = httpx.Client(
            timeout=config.REQUEST_TIMEOUT,
            follow_redirects=True,
            headers={
                "User-Agent": config.USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-GB,en;q=0.9",
                "Origin": "https://www.wizzair.com",
                "Referer": "https://www.wizzair.com/",
            },
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> WizzClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- version discovery --------------------------------------------------

    def api_version(self, force: bool = False) -> str:
        with self._version_lock:
            if self._version and not force:
                return self._version

            if not force:
                cached = self.cache.get("api_version", config.VERSION_TTL)
                if cached:
                    self._version = str(cached)
                    return self._version

            version = self._scrape_version()
            self._version = version
            self.cache.set("api_version", version)
            return version

    def _scrape_version(self) -> str:
        if self.offline:
            return str(self.cache.get_stale("api_version") or config.FALLBACK_API_VERSION)
        try:
            self._limiter.wait()
            resp = self._http.get(config.HOMEPAGE)
            resp.raise_for_status()
            match = _VERSION_RE.search(resp.text)
            if match:
                log.debug("discovered API version %s", match.group(1))
                return match.group(1)
            log.warning("no API version found in homepage HTML")
        except httpx.HTTPError as exc:
            log.warning("version discovery failed: %s", exc)

        stale = self.cache.get_stale("api_version")
        return str(stale or config.FALLBACK_API_VERSION)

    # --- transport ----------------------------------------------------------

    def _antiforgery_headers(self) -> dict[str, str]:
        """Echo the anti-forgery cookie back as a header.

        The backend hands out an ASP.NET ``RequestVerificationToken`` cookie on
        the first response and then expects the classic double-submit: the same
        value in an ``X-RequestVerificationToken`` header. Return the cookie
        without the header and every subsequent call fails with
        ``{"handlerError": "InvalidProtocol"}``. One-shot clients never notice;
        anything that keeps a cookie jar breaks after exactly one request.
        """
        token = self._http.cookies.get("RequestVerificationToken")
        return {"X-RequestVerificationToken": token} if token else {}

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Call the backend, retrying transient failures and version rotation."""
        if self.offline:
            raise WizzError(f"offline mode: refusing network call to {path}")

        last_exc: Exception | None = None
        for attempt in range(config.MAX_RETRIES):
            url = f"{config.BACKEND}/{self.api_version()}/Api/{path}"
            extra_headers = kwargs.pop("headers", {})
            try:
                self._limiter.wait()
                with self._request_lock:
                    headers = {**extra_headers, **self._antiforgery_headers()}
                    resp = self._http.request(method, url, headers=headers, **kwargs)
            except httpx.HTTPError as exc:
                last_exc = exc
                self._sleep_backoff(attempt)
                continue

            if resp.status_code == 400 and "InvalidProtocol" in resp.text:
                # Token and cookie are out of step. Drop the jar and start over
                # with a clean session rather than guessing at the mismatch.
                log.info("anti-forgery token rejected, resetting session")
                self._http.cookies.clear()
                last_exc = WizzError(f"InvalidProtocol for {url}")
                continue

            if resp.status_code == 404:
                # Almost always the version path having rotated under us.
                log.info("404 on %s, refreshing API version", url)
                self.api_version(force=True)
                last_exc = WizzError(f"404 for {url}")
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                last_exc = WizzError(f"HTTP {resp.status_code} for {url}")
                self._sleep_backoff(attempt)
                continue

            if resp.status_code >= 400:
                raise WizzError(f"HTTP {resp.status_code} for {url}: {resp.text[:200]}")

            try:
                return resp.json()
            except ValueError as exc:
                raise WizzError(f"non-JSON response from {url}") from exc

        raise WizzError(f"giving up on {path} after {config.MAX_RETRIES} attempts") from last_exc

    @staticmethod
    def _sleep_backoff(attempt: int) -> None:
        time.sleep(config.BACKOFF_BASE**attempt)

    # --- endpoints ----------------------------------------------------------

    def route_map(self, refresh: bool = False) -> dict[str, Any]:
        """The full network: every city, its coordinates, and its connections."""
        if not refresh:
            cached = self.cache.get("route_map", config.MAP_TTL)
            if cached:
                return cached

        if self.offline:
            stale = self.cache.get_stale("route_map")
            if stale:
                return stale
            raise WizzError("offline mode and no cached route map available")

        try:
            data = self._request(
                "GET", "asset/map", params={"languageCode": "en-gb"}
            )
        except WizzError:
            stale = self.cache.get_stale("route_map")
            if stale:
                log.warning("route map fetch failed, using stale cache")
                return stale
            raise

        self.cache.set("route_map", data)
        return data

    def timetable(
        self,
        origin: str,
        destination: str,
        date_from: date,
        date_to: date,
        refresh: bool = False,
    ) -> list[dict[str, Any]]:
        """Scheduled departures for one route over a date range.

        The endpoint treats a second ``flightList`` entry as the *return* leg,
        not as a second route, so this deliberately sends one route per call.
        Long spans come back silently truncated, hence the chunking.
        """
        out: list[dict[str, Any]] = []
        span = timedelta(days=config.TIMETABLE_MAX_SPAN_DAYS)
        chunk_start = date_from
        while chunk_start <= date_to:
            chunk_end = min(chunk_start + span, date_to)
            out.extend(
                self._timetable_chunk(
                    origin, destination, chunk_start, chunk_end, refresh
                )
            )
            chunk_start = chunk_end + timedelta(days=1)
        return out

    def _timetable_chunk(
        self,
        origin: str,
        destination: str,
        date_from: date,
        date_to: date,
        refresh: bool,
    ) -> list[dict[str, Any]]:
        key = f"tt_{origin}_{destination}_{date_from}_{date_to}"
        if not refresh:
            cached = self.cache.get(key, config.TIMETABLE_TTL)
            if cached is not None:
                return cached

        if self.offline:
            stale = self.cache.get_stale(key)
            if stale is not None:
                return stale
            raise WizzError(f"offline mode and no cached timetable for {key}")

        payload = {
            "flightList": [
                {
                    "departureStation": origin,
                    "arrivalStation": destination,
                    "from": date_from.isoformat(),
                    "to": date_to.isoformat(),
                }
            ],
            "priceType": "regular",
            "adultCount": 1,
            "childCount": 0,
            "infantCount": 0,
        }

        try:
            data = self._request("POST", "search/timetable", json=payload)
        except WizzError as exc:
            stale = self.cache.get_stale(key)
            if stale is not None:
                log.warning("timetable %s->%s failed (%s), using stale cache",
                            origin, destination, exc)
                return stale
            # Deliberately not swallowed into an empty list: "I could not check
            # this leg" and "this leg has no flights" must never look alike to
            # something that decides whether to spend a trip credit.
            raise

        flights = data.get("outboundFlights") or []
        self.cache.set(key, flights)
        return flights
