"""Guarantees the local queue makes beyond the shared contract."""

from __future__ import annotations

import time

from hopwatch.jobs.sqlite_queue import SqliteJobQueue


def test_stale_receipt_cannot_ack(tmp_path) -> None:
    """A worker whose lease lapsed must not delete a message someone else holds."""
    q = SqliteJobQueue(tmp_path / "q.db", visibility_s=1)
    q.send({"job": "a"})
    [slow] = q.receive()
    time.sleep(1.2)
    [fresh] = q.receive()

    q.ack(slow)
    assert q.depth().in_flight == 1
    q.ack(fresh)
    assert q.depth().in_flight == 0


def test_two_handles_on_one_file_share_messages(tmp_path) -> None:
    producer = SqliteJobQueue(tmp_path / "q.db")
    consumer = SqliteJobQueue(tmp_path / "q.db")
    producer.send({"job": "a"})
    assert [m.body for m in consumer.receive()] == [{"job": "a"}]
