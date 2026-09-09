"""Building the storage, queue, rate limit and cache from settings."""

from __future__ import annotations

from hopwatch import config
from hopwatch.backend import make_client, open_backend
from hopwatch.cache import DiskCache
from hopwatch.jobs.sqlite_queue import SqliteJobQueue
from hopwatch.ratelimit import SqliteRateLimiter
from hopwatch.settings import Settings
from hopwatch.store import SqliteStore


def local_settings(tmp_path) -> Settings:
    settings = Settings.load(tmp_path / "missing.toml")
    settings.database = tmp_path / "hw.db"
    return settings


def test_open_backend_local(tmp_path) -> None:
    backend = open_backend(local_settings(tmp_path))
    try:
        assert isinstance(backend.store, SqliteStore)
        assert isinstance(backend.queue, SqliteJobQueue)
        assert isinstance(backend.limiter, SqliteRateLimiter)
        assert isinstance(backend.cache, DiskCache)
        assert backend.limiter.min_interval == config.MIN_REQUEST_INTERVAL
        # One file for everything local: nothing extra to back up or mount.
        assert backend.queue.path == tmp_path / "hw.db"
    finally:
        backend.close()


def test_queue_settings_reach_the_local_queue(tmp_path) -> None:
    settings = local_settings(tmp_path)
    settings.queue.visibility_s = 7
    settings.queue.max_receives = 3
    backend = open_backend(settings)
    try:
        assert backend.queue.visibility_s == 7
        assert backend.queue.max_receives == 3
    finally:
        backend.close()


def test_make_client_shares_limiter_cache_and_overrides(tmp_path) -> None:
    settings = local_settings(tmp_path)
    settings.wizz.backend_url = "http://127.0.0.1:9999"
    settings.wizz.homepage_url = "http://127.0.0.1:9999/en-gb"
    backend = open_backend(settings)
    try:
        client = make_client(settings, backend)
        assert client._limiter is backend.limiter
        assert client.cache is backend.cache
        assert client.backend_url == "http://127.0.0.1:9999"
        assert client.homepage_url == "http://127.0.0.1:9999/en-gb"
        client.close()
    finally:
        backend.close()
