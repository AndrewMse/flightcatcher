"""The home auto-updater must never restart the booker in the middle of a booking.

``docker`` and ``curl`` are replaced by stubs on PATH that answer from
environment variables and record how they were called.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "hopwatch-update.sh"

DOCKER = """#!/bin/sh
echo "docker $*" >> "$CALLS"
case "$*" in
  "compose pull"*) exit 0 ;;
  "compose config --images"*) echo "ghcr.io/andrewmse/hopwatch:latest" ;;
  "image inspect"*) echo "$LATEST_ID" ;;
  "compose ps -q"*) echo "$CONTAINER" ;;
  "inspect"*) echo "$RUNNING_ID" ;;
  "compose up"*) exit 0 ;;
  "image prune"*) exit 0 ;;
esac
"""

CURL = """#!/bin/sh
echo "curl $*" >> "$CALLS"
if [ -z "$STATUS_JSON" ]; then exit 7; fi
echo "$STATUS_JSON"
"""


@pytest.fixture
def run(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("docker", DOCKER), ("curl", CURL)):
        path = bin_dir / name
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    calls = tmp_path / "calls.log"

    def invoke(*, latest="sha256:new", running="sha256:old", status=None, container="c1"):
        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "CALLS": str(calls),
            "COMPOSE_DIR": str(tmp_path),
            "LATEST_ID": latest,
            "RUNNING_ID": running,
            "CONTAINER": container,
            "STATUS_JSON": json.dumps(status) if status is not None else "",
        }
        result = subprocess.run(
            ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30
        )
        log = calls.read_text() if calls.exists() else ""
        calls.unlink(missing_ok=True)
        return result, log

    return invoke


def safe(flag: bool) -> dict:
    return {"watcher": {"safe_to_restart": flag, "open_bookings": 0 if flag else 1}}


def test_restarts_when_safe(run) -> None:
    result, calls = run(status=safe(True))
    assert result.returncode == 0, result.stderr
    assert "compose up -d" in calls
    assert "updated" in result.stdout


def test_defers_while_a_booking_is_open(run) -> None:
    result, calls = run(status=safe(False))
    assert result.returncode == 0, result.stderr
    assert "compose up" not in calls
    assert "deferred" in result.stdout


def test_nothing_to_do_when_already_current(run) -> None:
    result, calls = run(latest="sha256:same", running="sha256:same", status=safe(True))
    assert result.returncode == 0, result.stderr
    assert "compose up" not in calls
    assert "curl" not in calls
    assert "up to date" in result.stdout


def test_starts_the_service_when_it_is_not_running(run) -> None:
    result, calls = run(container="", status=None)
    assert result.returncode == 0, result.stderr
    assert "compose up -d" in calls


def test_defers_when_a_running_booker_cannot_answer(run) -> None:
    """Running but silent might be mid-confirm with the web UI off: never restart blind.

    A restart between two confirmed legs is the one way an update could
    strand someone, so "cannot tell" means "wait", not "go".
    """
    result, calls = run(status=None)
    assert result.returncode == 0, result.stderr
    assert "compose up" not in calls
    assert "deferred" in result.stdout


def test_defers_when_status_is_garbled(run) -> None:
    """Unreadable status is not permission: wait for a clear yes."""
    result, calls = run(status={"watcher": {}})
    assert result.returncode == 0, result.stderr
    assert "compose up" not in calls
    assert "deferred" in result.stdout
