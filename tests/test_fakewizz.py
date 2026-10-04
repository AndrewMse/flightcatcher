"""The benchmark's stand-in for Wizz Air must look like Wizz to the real client."""

from __future__ import annotations

from datetime import date, timedelta

from fastapi.testclient import TestClient

from bench.fakewizz import VERSION, build_app, serve_in_thread
from hopwatch.client import WizzClient
from hopwatch.network import RouteNetwork
from hopwatch.ratelimit import MemoryRateLimiter
from hopwatch.search import SearchOptions, search


def test_fake_map_parses_into_route_network() -> None:
    raw = TestClient(build_app(stations=40)).get(f"/{VERSION}/Api/asset/map").json()
    net = RouteNetwork(raw)
    assert len(net.stations) == 40
    codes = sorted(net.stations)
    assert net.find_paths([codes[0]], [codes[-1]], max_stops=2, max_detour=5.0)


def test_fake_is_deterministic_per_seed() -> None:
    def snapshot(seed: int) -> tuple:
        client = TestClient(build_app(seed=seed, stations=30, latency_ms=0))
        raw = client.get(f"/{VERSION}/Api/asset/map").json()
        first = raw["cities"][0]
        route = (first["iata"], first["connections"][0]["iata"])
        body = {"flightList": [{"departureStation": route[0], "arrivalStation": route[1],
                                "from": "2026-11-02", "to": "2026-11-09"}]}
        tt = client.post(f"/{VERSION}/Api/search/timetable", json=body).json()
        return raw, tt

    assert snapshot(7) == snapshot(7)
    assert snapshot(7) != snapshot(8)


def test_fake_counts_requests_and_resets() -> None:
    client = TestClient(build_app(stations=20, latency_ms=0))
    client.get("/en-gb")
    client.get(f"/{VERSION}/Api/asset/map")
    stats = client.get("/_stats").json()
    assert stats["total"] == 2
    assert stats["by_endpoint"] == {"homepage": 1, "map": 1}
    client.post("/_reset")
    assert client.get("/_stats").json()["total"] == 0


def test_fake_injects_errors() -> None:
    client = TestClient(build_app(stations=20, latency_ms=0, error_rate=1.0))
    assert client.get(f"/{VERSION}/Api/asset/map").status_code == 503
    client = TestClient(build_app(stations=20, latency_ms=0, throttle_rate=1.0))
    assert client.get(f"/{VERSION}/Api/asset/map").status_code == 429


def test_real_client_and_search_work_against_the_fake(tmp_path) -> None:
    app = build_app(stations=40, latency_ms=1)
    url, stop = serve_in_thread(app)
    try:
        client = WizzClient(
            cache_dir=tmp_path, limiter=MemoryRateLimiter(0),
            backend_url=url, homepage_url=f"{url}/en-gb",
        )
        net = RouteNetwork(client.route_map())
        codes = sorted(net.stations)
        start = date.today() + timedelta(days=2)
        opts = SearchOptions(date_from=start, date_to=start + timedelta(days=6), max_stops=1,
                             max_detour=4.0, include_closed=True, limit=500)
        found = 0
        for origin, dest in zip(codes, reversed(codes)):
            result = search(client, net, [origin], [dest], opts)
            assert result.is_complete
            found += len(result.itineraries)
        assert found > 0
        assert client.api_version() == VERSION
        client.close()
    finally:
        stop()
