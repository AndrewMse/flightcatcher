"""Carry FlightCatcher-era files and environment variables over to Hopwatch.

The project was renamed. Its state did not get any less precious: the database
holds every want and booking, and the browser profile holds a logged-in Wizz
session that can take a human to re-create. Starting empty at the new paths
would look like a fresh install and silently orphan both.

The rule is deliberately narrow. A path is moved only when the new location is
missing and the old-named sibling exists, and nothing is ever overwritten. A
config file that still names the old paths explicitly keeps working as is.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Sequence

log = logging.getLogger(__name__)

NEW = "hopwatch"
OLD = "flightcatcher"

_warned_env: set[str] = set()


def legacy_path(path: Path) -> Path:
    """The FlightCatcher-era name for ``path``.

    Only the file name and its directory are renamed. Ancestors further up
    belong to the system, not to this program: a service account called
    ``hopwatch`` keeps its home directory.
    """
    parts = list(path.parts)
    for i in range(max(0, len(parts) - 2), len(parts)):
        parts[i] = parts[i].replace(NEW, OLD)
    return Path(*parts)


def migrate_path(path: Path, *, companions: Sequence[str] = ()) -> bool:
    """Move the legacy file or directory to ``path`` if only the legacy exists.

    ``companions`` are suffixes of files that travel with the main one, such
    as SQLite's ``-wal`` and ``-shm``. Returns True when something was moved.
    """
    old = legacy_path(path)
    if old == path or path.exists() or not old.exists():
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(old), str(path))
    for suffix in companions:
        old_companion = old.with_name(old.name + suffix)
        new_companion = path.with_name(path.name + suffix)
        if old_companion.exists() and not new_companion.exists():
            shutil.move(str(old_companion), str(new_companion))

    log.warning("moved %s to %s (project renamed to Hopwatch)", old, path)
    return True


def env(name: str) -> str | None:
    """Read ``HOPWATCH_<name>``, falling back to ``FLIGHTCATCHER_<name>``."""
    value = os.environ.get(f"HOPWATCH_{name}")
    if value:
        return value
    legacy = os.environ.get(f"FLIGHTCATCHER_{name}")
    if legacy and name not in _warned_env:
        _warned_env.add(name)
        log.warning("FLIGHTCATCHER_%s is deprecated, rename it to HOPWATCH_%s", name, name)
    return legacy or None
