"""The on-disk cache shared by every local worker process."""

from __future__ import annotations

import os

import pytest

from hopwatch.cache import DiskCache


def test_round_trip_and_ttl(tmp_path) -> None:
    cache = DiskCache(tmp_path)
    cache.set("tt_OTP_EIN", [{"a": 1}])
    assert cache.get("tt_OTP_EIN", ttl=60) == [{"a": 1}]
    assert cache.get("missing", ttl=60) is None


def test_expired_entries_are_only_available_as_stale(tmp_path) -> None:
    cache = DiskCache(tmp_path)
    cache.set("k", "v")
    path = next(tmp_path.iterdir())
    os.utime(path, (1, 1))
    assert cache.get("k", ttl=60) is None
    assert cache.get_stale("k") == "v"


def test_write_is_atomic(tmp_path, monkeypatch) -> None:
    """A writer dying mid-write must leave the previous value readable."""
    cache = DiskCache(tmp_path)
    cache.set("k", "old")

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    cache.set("k", "new")  # logged, never raised
    monkeypatch.undo()
    assert cache.get("k", ttl=60) == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["k.json"]
