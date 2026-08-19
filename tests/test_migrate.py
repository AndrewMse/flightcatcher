"""Carrying FlightCatcher-era state over to Hopwatch paths.

The browser profile holds a live Wizz session and the database holds every
want and booking, so a rename must move them rather than quietly start empty.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hopwatch import migrate


def test_legacy_path_swaps_the_directory_and_file_names() -> None:
    new = Path("/home/me/.local/share/hopwatch/hopwatch.db")
    assert migrate.legacy_path(new) == Path(
        "/home/me/.local/share/flightcatcher/flightcatcher.db"
    )


def test_legacy_path_leaves_unrelated_ancestors_alone() -> None:
    """A service account called hopwatch must not turn into /home/flightcatcher."""
    new = Path("/home/hopwatch/.local/share/hopwatch/profile")
    assert migrate.legacy_path(new) == Path(
        "/home/hopwatch/.local/share/flightcatcher/profile"
    )


def test_moves_legacy_database_with_wal_files(tmp_path: Path) -> None:
    old_dir = tmp_path / "flightcatcher"
    old_dir.mkdir()
    for suffix in ("", "-wal", "-shm"):
        (old_dir / f"flightcatcher.db{suffix}").write_text(f"data{suffix}")

    new = tmp_path / "hopwatch" / "hopwatch.db"
    assert migrate.migrate_path(new, companions=("-wal", "-shm")) is True

    for suffix in ("", "-wal", "-shm"):
        assert (tmp_path / "hopwatch" / f"hopwatch.db{suffix}").read_text() == f"data{suffix}"
        assert not (old_dir / f"flightcatcher.db{suffix}").exists()


def test_moves_legacy_directory_keeping_mode(tmp_path: Path) -> None:
    old = tmp_path / "flightcatcher" / "profile"
    old.mkdir(parents=True)
    (old / "Cookies").write_text("session")
    os.chmod(old, 0o700)

    new = tmp_path / "hopwatch" / "profile"
    assert migrate.migrate_path(new) is True
    assert (new / "Cookies").read_text() == "session"
    assert new.stat().st_mode & 0o777 == 0o700


def test_never_overwrites_existing_new_path(tmp_path: Path) -> None:
    old = tmp_path / "flightcatcher" / "flightcatcher.db"
    old.parent.mkdir()
    old.write_text("old")
    new = tmp_path / "hopwatch" / "hopwatch.db"
    new.parent.mkdir()
    new.write_text("new")

    assert migrate.migrate_path(new) is False
    assert new.read_text() == "new"
    assert old.read_text() == "old"


def test_noop_without_legacy(tmp_path: Path) -> None:
    new = tmp_path / "hopwatch" / "hopwatch.db"
    assert migrate.migrate_path(new) is False
    assert not new.parent.exists()


def test_env_prefers_new_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOPWATCH_DB", "/new.db")
    monkeypatch.setenv("FLIGHTCATCHER_DB", "/old.db")
    assert migrate.env("DB") == "/new.db"


def test_env_falls_back_to_legacy_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOPWATCH_DB", raising=False)
    monkeypatch.setenv("FLIGHTCATCHER_DB", "/old.db")
    assert migrate.env("DB") == "/old.db"


def test_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOPWATCH_DB", raising=False)
    monkeypatch.delenv("FLIGHTCATCHER_DB", raising=False)
    assert migrate.env("DB") is None
