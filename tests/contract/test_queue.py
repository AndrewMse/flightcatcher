"""Queue semantics every backend shares: at-least-once, leases, dead letters.

These are SQS's rules. The local queue copies them on purpose, so a worker
that is correct on a laptop is correct on Lambda.
"""

from __future__ import annotations

import threading
import time


def test_send_receive_ack(make_queue) -> None:
    q = make_queue()
    q.send({"job": "a"})
    [msg] = q.receive()
    assert msg.body == {"job": "a"}
    assert msg.receive_count == 1
    q.ack(msg)
    assert q.receive() == []
    assert q.depth().visible == 0
    assert q.depth().in_flight == 0


def test_received_message_is_hidden_while_leased(make_queue) -> None:
    q = make_queue(visibility_s=30)
    q.send({"job": "a"})
    assert len(q.receive()) == 1
    assert q.receive() == []
    depth = q.depth()
    assert (depth.visible, depth.in_flight) == (0, 1)


def test_unacked_message_reappears_after_visibility(make_queue) -> None:
    q = make_queue(visibility_s=1)
    q.send({"job": "a"})
    [first] = q.receive()
    time.sleep(1.2)
    [again] = q.receive()
    assert again.id == first.id
    assert again.receive_count == 2


def test_retry_later_delays_visibility(make_queue) -> None:
    q = make_queue(visibility_s=30)
    q.send({"job": "a"})
    [msg] = q.receive()
    q.retry_later(msg, delay_s=1)
    assert q.receive() == []
    time.sleep(1.2)
    [again] = q.receive()
    assert again.receive_count == 2


def test_dead_letters_after_max_receives(make_queue) -> None:
    q = make_queue(visibility_s=1, max_receives=2)
    q.send({"job": "poison"})
    for _ in range(2):
        [msg] = q.receive()
        q.retry_later(msg, delay_s=0)
    time.sleep(0.1)
    assert q.receive(wait_s=1.5) == []
    assert q.depth().dead == 1
    assert [m.body for m in q.dead_letters()] == [{"job": "poison"}]


def test_delay_on_send(make_queue) -> None:
    q = make_queue()
    q.send({"job": "later"}, delay_s=1)
    assert q.receive() == []
    time.sleep(1.2)
    assert [m.body for m in q.receive()] == [{"job": "later"}]


def test_wait_s_long_polls(make_queue) -> None:
    q = make_queue()
    threading.Timer(0.3, q.send, args=({"job": "late"},)).start()
    started = time.monotonic()
    msgs = q.receive(wait_s=3)
    assert [m.body for m in msgs] == [{"job": "late"}]
    assert time.monotonic() - started < 2.5


def test_receive_respects_max_messages(make_queue) -> None:
    q = make_queue()
    for i in range(3):
        q.send({"job": str(i)})
    assert len(q.receive(max_messages=2)) == 2
    assert len(q.receive(max_messages=2)) == 1
