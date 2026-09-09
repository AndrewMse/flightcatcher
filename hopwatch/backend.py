"""Where state, jobs, the rate limit and the cache live, chosen by settings.

Everything above this module talks to protocols. This is the one place that
knows "local" means SQLite on this machine and "aws" means DynamoDB and SQS.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass

from . import config
from .cache import Cache, DiskCache
from .client import WizzClient
from .jobs.queue import JobQueue
from .jobs.sqlite_queue import SqliteJobQueue
from .ratelimit import RateLimiter, SqliteRateLimiter
from .settings import Settings
from .store import SqliteStore, Store


class ConfigError(RuntimeError):
    """The settings cannot produce a working backend."""


@dataclass
class Backend:
    store: Store
    queue: JobQueue
    limiter: RateLimiter
    cache: Cache

    def close(self) -> None:
        for part in (self.queue, self.limiter, self.store):
            close = getattr(part, "close", None)
            if close:
                with contextlib.suppress(Exception):
                    close()


def open_backend(settings: Settings) -> Backend:
    mode = settings.backend.mode
    if mode == "local":
        return _local(settings)
    if mode == "aws":
        return _aws(settings)
    raise ConfigError(f"backend.mode must be 'local' or 'aws', not {mode!r}")


def _local(settings: Settings) -> Backend:
    # One SQLite file holds state, the queue and the shared rate limit, so a
    # local deployment has exactly one thing to back up or mount.
    db = settings.database
    return Backend(
        store=SqliteStore(db),
        queue=SqliteJobQueue(
            db,
            visibility_s=settings.queue.visibility_s,
            max_receives=settings.queue.max_receives,
        ),
        limiter=SqliteRateLimiter(db, config.MIN_REQUEST_INTERVAL),
        cache=DiskCache(config.CACHE_DIR),
    )


def _aws(settings: Settings) -> Backend:
    raise ConfigError("backend.mode = 'aws' is not available in this build")


def make_client(settings: Settings, backend: Backend) -> WizzClient:
    """A Wizz client that spends from the shared politeness budget."""
    return WizzClient(
        cache=backend.cache,
        limiter=backend.limiter,
        backend_url=settings.wizz.backend_url or None,
        homepage_url=settings.wizz.homepage_url or None,
    )
