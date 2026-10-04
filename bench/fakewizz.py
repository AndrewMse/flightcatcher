"""A stand-in for Wizz Air's public endpoints, for benchmarks only.

The real site is never benchmarked: hammering it is exactly what the
politeness limits exist to prevent, and its latency is not ours to measure.
This serves the three endpoints the client uses, shaped like the real
payloads, from a seeded synthetic network:

    GET  /en-gb                          homepage carrying the API version
    GET  /{version}/Api/asset/map        the route network
    POST /{version}/Api/search/timetable departures for one route

plus ``/_stats`` and ``/_reset`` so a benchmark can count what it cost.
Latency, 5xx and 429 rates are configurable. Everything is deterministic for
a given seed, so two runs search the same world.

    python -m bench.fakewizz --port 8999
"""

from __future__ import annotations

import argparse
import asyncio
import random
import secrets
import string
import threading
import time
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any, Callable

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.requests import ClientDisconnect

VERSION = "30.0.0"

# Single-timezone countries the real timezone table knows about.
COUNTRIES = {
    "RO": "Romania", "PL": "Poland", "HU": "Hungary", "NL": "Netherlands",
    "ES": "Spain", "IT": "Italy", "DE": "Germany", "FR": "France",
    "GB": "United Kingdom", "BG": "Bulgaria", "PT": "Portugal", "SE": "Sweden",
    "GR": "Greece", "CZ": "Czechia", "LT": "Lithuania",
}


def build_network(seed: int, stations: int) -> list[dict[str, Any]]:
    """A hub-and-spoke network, roughly the shape of Wizz's."""
    rng = random.Random(f"network:{seed}")
    codes: list[str] = []
    while len(codes) < stations:
        code = "".join(rng.choice(string.ascii_uppercase) for _ in range(3))
        if code not in codes:
            codes.append(code)

    # Like Wizz: a handful of bases, each linked to most others, and smaller
    # airports each served from several bases. A one-stop search between two
    # smaller airports then has a realistic dozen or so ways through.
    hubs = codes[: max(4, stations // 5)]
    links: dict[str, set[str]] = {code: set() for code in codes}

    def link(a: str, b: str) -> None:
        if a != b:
            links[a].add(b)
            links[b].add(a)

    for i, hub in enumerate(hubs):
        for other in hubs[i + 1:]:
            if rng.random() < 0.7:
                link(hub, other)
    for code in codes[len(hubs):]:
        for hub in rng.sample(hubs, k=min(len(hubs), rng.randint(5, 9))):
            link(code, hub)
        if rng.random() < 0.4:
            link(code, rng.choice(codes[len(hubs):]))

    country_codes = sorted(COUNTRIES)
    cities = []
    for code in codes:
        country = rng.choice(country_codes)
        cities.append({
            "iata": code,
            "shortName": f"Fakeport {code}",
            "countryCode": country,
            "countryName": COUNTRIES[country],
            "latitude": round(rng.uniform(37.0, 58.0), 4),
            "longitude": round(rng.uniform(-8.0, 28.0), 4),
            "mac": None,
            "connections": [
                {"iata": dest, "operationStartDate": None, "isDirectFlight": True}
                for dest in sorted(links[code])
            ],
        })
    return cities


def schedule_for(seed: int, origin: str, dest: str) -> tuple[set[int], list[tuple[int, int]], float]:
    """Which weekdays a route flies, at what local times, and its price."""
    rng = random.Random(f"route:{seed}:{origin}:{dest}")
    weekdays = set(rng.sample(range(7), k=rng.randint(3, 7)))
    times = sorted(
        (rng.randint(6, 22), rng.choice((0, 15, 30, 45))) for _ in range(rng.randint(1, 2))
    )
    return weekdays, times, round(rng.uniform(10, 180), 2)


def timetable(seed: int, origin: str, dest: str, start: date, end: date) -> list[dict[str, Any]]:
    weekdays, times, price = schedule_for(seed, origin, dest)
    flights = []
    day = start
    while day <= end:
        if day.weekday() in weekdays:
            flights.append({
                "departureStation": origin,
                "arrivalStation": dest,
                "departureDates": [
                    datetime(day.year, day.month, day.day, hour, minute).isoformat()
                    for hour, minute in times
                ],
                "price": {"amount": price, "currencyCode": "EUR"},
                "priceType": "price",
                "originalPrice": {"amount": price, "currencyCode": "EUR"},
            })
        day += timedelta(days=1)
    return flights


class Stats:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with getattr(self, "lock", threading.Lock()):
            self.by_endpoint: Counter[str] = Counter()
            self.stamps: list[float] = []

    def hit(self, endpoint: str) -> None:
        with self.lock:
            self.by_endpoint[endpoint] += 1
            self.stamps.append(time.time())

    def snapshot(self, with_stamps: bool) -> dict[str, Any]:
        with self.lock:
            stamps = sorted(self.stamps)
            data: dict[str, Any] = {
                "total": len(stamps),
                "by_endpoint": dict(self.by_endpoint),
                "max_per_second": _max_in_window(stamps, 1.0),
            }
            if with_stamps:
                data["timestamps"] = stamps
            return data


def _max_in_window(stamps: list[float], window: float) -> int:
    best, left = 0, 0
    for right, stamp in enumerate(stamps):
        while stamp - stamps[left] >= window:
            left += 1
        best = max(best, right - left + 1)
    return best


def build_app(
    seed: int = 7,
    stations: int = 60,
    latency_ms: float = 120.0,
    error_rate: float = 0.0,
    throttle_rate: float = 0.0,
) -> FastAPI:
    app = FastAPI(title="fake wizz", docs_url=None, redoc_url=None)
    cities = build_network(seed, stations)
    stats = Stats()
    chaos = random.Random(f"chaos:{seed}")

    async def behave(endpoint: str) -> Response | None:
        stats.hit(endpoint)
        if latency_ms > 0:
            await asyncio.sleep(max(0.0, chaos.gauss(latency_ms, latency_ms / 4)) / 1000)
        roll = chaos.random()
        if roll < error_rate:
            return JSONResponse({"error": "injected"}, status_code=503)
        if roll < error_rate + throttle_rate:
            return JSONResponse({"error": "slow down"}, status_code=429)
        return None

    def with_cookie(response: Response) -> Response:
        response.set_cookie("RequestVerificationToken", secrets.token_hex(8))
        return response

    @app.get("/en-gb")
    async def homepage() -> Response:
        failed = await behave("homepage")
        if failed:
            return failed
        html = f'<script src="https://be.wizzair.com/{VERSION}/Api/x.js"></script>'
        return with_cookie(HTMLResponse(html))

    @app.get(f"/{VERSION}/Api/asset/map")
    async def route_map() -> Response:
        failed = await behave("map")
        if failed:
            return failed
        return with_cookie(JSONResponse({"cities": cities}))

    @app.post(f"/{VERSION}/Api/search/timetable")
    async def search_timetable(request: Request) -> Response:
        failed = await behave("timetable")
        if failed:
            return failed
        try:
            body = await request.json()
        except ClientDisconnect:
            # The recovery benchmark SIGKILLs workers mid-request; that is the point.
            return Response(status_code=499)
        leg = body["flightList"][0]
        flights = timetable(
            seed,
            leg["departureStation"],
            leg["arrivalStation"],
            date.fromisoformat(leg["from"]),
            date.fromisoformat(leg["to"]),
        )
        return with_cookie(JSONResponse({"outboundFlights": flights, "returnFlights": []}))

    @app.get("/_stats")
    async def get_stats(timestamps: bool = False) -> dict[str, Any]:
        return stats.snapshot(timestamps)

    @app.post("/_reset")
    async def reset() -> dict[str, bool]:
        stats.reset()
        return {"ok": True}

    return app


def serve_in_thread(app: FastAPI, port: int = 0) -> tuple[str, Callable[[], None]]:
    """Run ``app`` on localhost in a background thread. Returns (url, stop)."""
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("fake wizz did not start")
        time.sleep(0.02)
    bound = server.servers[0].sockets[0].getsockname()[1]

    def stop() -> None:
        server.should_exit = True
        thread.join(timeout=5)

    return f"http://127.0.0.1:{bound}", stop


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8999)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--stations", type=int, default=60)
    parser.add_argument("--latency-ms", type=float, default=120.0)
    parser.add_argument("--error-rate", type=float, default=0.0)
    parser.add_argument("--throttle-rate", type=float, default=0.0)
    args = parser.parse_args()
    app = build_app(args.seed, args.stations, args.latency_ms, args.error_rate, args.throttle_rate)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
