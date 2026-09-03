"""Response cache for the unauthenticated Wizz endpoints.

Timetables are the expensive part of a sweep and the same route is wanted by
many itineraries, so the cache is what makes extra workers cheap: one worker's
fetch serves the rest. ``get_stale`` returns an entry regardless of age, for
when the network fails and an old answer beats no answer.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)


class Cache(Protocol):
    def get(self, key: str, ttl: float) -> Any | None: ...
    def set(self, key: str, value: Any) -> None: ...
    def get_stale(self, key: str) -> Any | None: ...


class DiskCache:
    """One JSON file per key. Safe to share between local worker processes."""

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
        # Write beside the target and rename over it: another process reading
        # at the same moment sees the old file or the new one, never half.
        target = self._path(key)
        tmp_name = None
        try:
            fd, tmp_name = tempfile.mkstemp(dir=self.root, prefix=".tmp-", suffix=".json")
            with os.fdopen(fd, "w") as handle:
                handle.write(json.dumps(value))
            os.replace(tmp_name, target)
            tmp_name = None
        except OSError as exc:  # a broken cache must never break a search
            log.warning("cache write failed for %s: %s", key, exc)
        finally:
            if tmp_name:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass

    def get_stale(self, key: str) -> Any | None:
        """Last known value regardless of age, for use when the network fails."""
        return self.get(key, ttl=float("inf"))
