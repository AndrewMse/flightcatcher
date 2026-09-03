from __future__ import annotations

from datetime import date

import httpx
import pytest

from hopwatch import config
from hopwatch.client import WizzClient, WizzError


@pytest.fixture
def client(tmp_path, monkeypatch) -> WizzClient:
    monkeypatch.setattr(config, "MIN_REQUEST_INTERVAL", 0.0)
    monkeypatch.setattr(config, "BACKOFF_BASE", 0.0)
    return WizzClient(cache_dir=tmp_path)


def mount(client: WizzClient, handler) -> None:
    """Swap the client's transport for a scripted one."""
    client._http = httpx.Client(
        transport=httpx.MockTransport(handler),
        headers=client._http.headers,
        cookies=client._http.cookies,
    )


def test_version_is_scraped_from_the_homepage(client: WizzClient) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "www.wizzair.com"
        return httpx.Response(
            200, text='<script src="https://be.wizzair.com/31.2.7/Api/foo"></script>'
        )

    mount(client, handler)
    assert client.api_version() == "31.2.7"
    # Cached on disk, so a second call needs no request.
    assert client.cache.get("api_version", ttl=3600) == "31.2.7"


def test_version_falls_back_when_the_homepage_is_unhelpful(client: WizzClient) -> None:
    mount(client, lambda request: httpx.Response(200, text="<html>nothing here</html>"))
    assert client.api_version() == config.FALLBACK_API_VERSION


def test_antiforgery_token_is_echoed_back_as_a_header(client: WizzClient) -> None:
    """The real backend 400s on any request that returns the cookie without the header."""
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.wizzair.com":
            return httpx.Response(200, text="be.wizzair.com/1.0.0")

        cookie = request.headers.get("cookie", "")
        header = request.headers.get("x-requestverificationtoken")
        seen.append(header)

        if "RequestVerificationToken" in cookie and not header:
            return httpx.Response(400, json={"handlerError": "InvalidProtocol"})

        return httpx.Response(
            200,
            json={"outboundFlights": []},
            headers={"set-cookie": "RequestVerificationToken=abc123; path=/"},
        )

    mount(client, handler)
    for _ in range(3):
        client.timetable("OTP", "EIN", date(2026, 9, 16), date(2026, 9, 17), refresh=True)

    assert seen[0] is None  # nothing to echo on the first call
    assert seen[1:] == ["abc123", "abc123"]


def test_invalid_protocol_resets_the_session_and_retries(client: WizzClient) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.wizzair.com":
            return httpx.Response(200, text="be.wizzair.com/1.0.0")
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(400, json={"handlerError": "InvalidProtocol"})
        return httpx.Response(200, json={"outboundFlights": [{"departureStation": "OTP"}]})

    mount(client, handler)
    flights = client.timetable("OTP", "EIN", date(2026, 9, 16), date(2026, 9, 17))
    assert flights == [{"departureStation": "OTP"}]
    assert calls["n"] == 2


def test_404_triggers_a_version_refresh(client: WizzClient) -> None:
    versions: list[str] = []
    homepage_hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.wizzair.com":
            homepage_hits["n"] += 1
            version = "1.0.0" if homepage_hits["n"] == 1 else "2.0.0"
            return httpx.Response(200, text=f"be.wizzair.com/{version}")
        versions.append(request.url.path.split("/")[1])
        if versions[-1] == "1.0.0":
            return httpx.Response(404)
        return httpx.Response(200, json={"outboundFlights": []})

    mount(client, handler)
    client.timetable("OTP", "EIN", date(2026, 9, 16), date(2026, 9, 17))
    assert versions == ["1.0.0", "2.0.0"]


def test_timetable_chunks_long_ranges(client: WizzClient, monkeypatch) -> None:
    monkeypatch.setattr(config, "TIMETABLE_MAX_SPAN_DAYS", 10)
    spans: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.wizzair.com":
            return httpx.Response(200, text="be.wizzair.com/1.0.0")
        import json

        leg = json.loads(request.content)["flightList"][0]
        spans.append((leg["from"], leg["to"]))
        return httpx.Response(200, json={"outboundFlights": []})

    mount(client, handler)
    client.timetable("OTP", "EIN", date(2026, 9, 1), date(2026, 10, 5))

    assert len(spans) == 4
    assert spans[0] == ("2026-09-01", "2026-09-11")
    assert spans[-1][1] == "2026-10-05"


def test_timetable_failure_raises_rather_than_looking_empty(client: WizzClient) -> None:
    """"Could not check" must never be indistinguishable from "no flights"."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.wizzair.com":
            return httpx.Response(200, text="be.wizzair.com/1.0.0")
        return httpx.Response(503)

    mount(client, handler)
    with pytest.raises(WizzError):
        client.timetable("OTP", "EIN", date(2026, 9, 16), date(2026, 9, 17))


def test_timetable_failure_prefers_stale_cache_over_failing(client: WizzClient) -> None:
    ok = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.wizzair.com":
            return httpx.Response(200, text="be.wizzair.com/1.0.0")
        ok["n"] += 1
        if ok["n"] == 1:
            return httpx.Response(200, json={"outboundFlights": [{"departureStation": "OTP"}]})
        return httpx.Response(503)

    mount(client, handler)
    window = (date(2026, 9, 16), date(2026, 9, 17))
    assert client.timetable("OTP", "EIN", *window) == [{"departureStation": "OTP"}]
    # Forced refresh fails, but a stale answer beats no answer.
    assert client.timetable("OTP", "EIN", *window, refresh=True) == [
        {"departureStation": "OTP"}
    ]


def test_empty_timetable_is_a_real_answer_and_is_cached(client: WizzClient) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.wizzair.com":
            return httpx.Response(200, text="be.wizzair.com/1.0.0")
        return httpx.Response(200, json={"outboundFlights": []})

    mount(client, handler)
    assert client.timetable("OTP", "EIN", date(2026, 9, 16), date(2026, 9, 17)) == []


def test_offline_mode_makes_no_network_calls(tmp_path) -> None:
    offline = WizzClient(cache_dir=tmp_path, offline=True)
    mount(offline, lambda request: pytest.fail("offline client made a request"))
    with pytest.raises(WizzError):
        offline.route_map()


def test_client_counts_requests_and_cache_hits(client: WizzClient) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.host == "www.wizzair.com":
            return httpx.Response(200, text="be.wizzair.com/31.2.7/Api")
        return httpx.Response(200, json={"outboundFlights": []})

    mount(client, handler)
    client.timetable("OTP", "EIN", date(2026, 9, 1), date(2026, 9, 3))
    client.timetable("OTP", "EIN", date(2026, 9, 1), date(2026, 9, 3))

    assert len(calls) == 2  # homepage scrape + one timetable
    assert client.stats.requests == 2
    assert client.stats.cache_hits == 1


def test_client_uses_backend_and_homepage_overrides(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "MIN_REQUEST_INTERVAL", 0.0)
    custom = WizzClient(
        cache_dir=tmp_path,
        backend_url="http://fake.local:9000",
        homepage_url="http://fake.local:9000/en-gb",
    )
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/en-gb":
            return httpx.Response(200, text="be.wizzair.com/30.0.0/Api")
        return httpx.Response(200, json={"outboundFlights": []})

    mount(custom, handler)
    custom.timetable("OTP", "EIN", date(2026, 9, 1), date(2026, 9, 2))
    assert seen[0] == "http://fake.local:9000/en-gb"
    assert seen[1] == "http://fake.local:9000/30.0.0/Api/search/timetable"


def test_client_accepts_injected_cache_and_limiter(tmp_path) -> None:
    class Limiter:
        waits = 0

        def wait(self) -> None:
            Limiter.waits += 1

    class Cache:
        def __init__(self) -> None:
            self.data = {"route_map": {"cities": []}}

        def get(self, key, ttl):
            return self.data.get(key)

        def set(self, key, value) -> None:
            self.data[key] = value

        def get_stale(self, key):
            return self.data.get(key)

    injected = WizzClient(cache=Cache(), limiter=Limiter())
    assert injected.route_map() == {"cities": []}
    assert injected.stats.cache_hits == 1
    assert Limiter.waits == 0
