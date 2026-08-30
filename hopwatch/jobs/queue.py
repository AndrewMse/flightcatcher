"""The queue contract: SQS semantics, whatever is underneath.

At-least-once delivery, a visibility timeout that hides a received message
until it is acknowledged or its lease runs out, and a dead-letter area for
messages received too many times. Workers are written against exactly these
rules, which is why a duplicate delivery or a crashed worker is routine rather
than a special case.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol


@dataclass
class Message:
    id: str
    body: dict[str, Any]
    receive_count: int
    receipt: str
    sent_at: datetime


@dataclass
class QueueDepth:
    visible: int
    in_flight: int
    dead: int


class JobQueue(Protocol):
    def send(self, body: dict[str, Any], delay_s: int = 0) -> None: ...
    def receive(self, max_messages: int = 1, wait_s: float = 0.0) -> list[Message]: ...
    def ack(self, message: Message) -> None: ...
    def retry_later(self, message: Message, delay_s: int) -> None: ...
    def depth(self) -> QueueDepth: ...
    def dead_letters(self, limit: int = 20) -> list[Message]: ...
    def close(self) -> None: ...
