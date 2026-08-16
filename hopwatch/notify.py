"""Notification fan-out.

The watcher does not know or care where alerts go. It calls a ``Notifier``;
Discord, the event log, and anything added later are implementations.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol, Sequence

from .store import Booking, Candidate, Store

log = logging.getLogger(__name__)


class Notifier(Protocol):
    async def candidate_available(self, candidate: Candidate) -> None: ...
    async def approval_needed(self, booking: Booking, candidate: Candidate) -> None: ...
    async def booking_finished(self, booking: Booking, candidate: Candidate) -> None: ...
    async def problem(self, title: str, detail: str) -> None: ...


class NullNotifier:
    """Does nothing. Used when Discord is off."""

    async def candidate_available(self, candidate: Candidate) -> None:
        return None

    async def approval_needed(self, booking: Booking, candidate: Candidate) -> None:
        return None

    async def booking_finished(self, booking: Booking, candidate: Candidate) -> None:
        return None

    async def problem(self, title: str, detail: str) -> None:
        return None


class StoreNotifier:
    """Writes every notification into the event feed the web UI reads."""

    def __init__(self, store: Store) -> None:
        self.store = store

    async def candidate_available(self, candidate: Candidate) -> None:
        self.store.log(
            "available",
            f"{describe(candidate)} is bookable on Multipass",
            level="success",
            candidate_id=candidate.id,
        )

    async def approval_needed(self, booking: Booking, candidate: Candidate) -> None:
        self.store.log(
            "approval_needed",
            f"Approval needed for {describe(candidate)}",
            level="warning",
            booking_id=booking.id,
            candidate_id=candidate.id,
        )

    async def booking_finished(self, booking: Booking, candidate: Candidate) -> None:
        level = {"booked": "success", "failed": "error"}.get(booking.status, "info")
        self.store.log(
            "booking_finished",
            f"Booking {booking.id} for {describe(candidate)}: {booking.status}",
            level=level,
            booking_id=booking.id,
            candidate_id=candidate.id,
        )

    async def problem(self, title: str, detail: str) -> None:
        self.store.log("problem", f"{title}: {detail}", level="error")


class CompositeNotifier:
    """Fans out to several notifiers; one failing never blocks the others."""

    def __init__(self, notifiers: Sequence[Notifier]) -> None:
        self.notifiers = list(notifiers)

    async def _fan(self, method: str, *args: Any) -> None:
        async def call(notifier: Notifier) -> None:
            try:
                await getattr(notifier, method)(*args)
            except Exception as exc:  # noqa: BLE001
                log.warning("%s.%s failed: %s", type(notifier).__name__, method, exc)

        await asyncio.gather(*(call(n) for n in self.notifiers))

    async def candidate_available(self, candidate: Candidate) -> None:
        await self._fan("candidate_available", candidate)

    async def approval_needed(self, booking: Booking, candidate: Candidate) -> None:
        await self._fan("approval_needed", booking, candidate)

    async def booking_finished(self, booking: Booking, candidate: Candidate) -> None:
        await self._fan("booking_finished", booking, candidate)

    async def problem(self, title: str, detail: str) -> None:
        await self._fan("problem", title, detail)


def describe(candidate: Candidate) -> str:
    path = " → ".join(candidate.path)
    when = candidate.legs[0]["departs_local"][:16].replace("T", " ") if candidate.legs else ""
    return f"{path} on {when}"
