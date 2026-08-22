"""The SQLite store is one implementation of a protocol the rest of the code uses."""

from __future__ import annotations

from hopwatch.store import SqliteStore, Store


def test_sqlite_store_satisfies_the_protocol(tmp_path) -> None:
    store = SqliteStore(tmp_path / "s.db")
    try:
        assert isinstance(store, Store)
    finally:
        store.close()
