"""Persistent state: records, the storage protocol and its implementations."""

from __future__ import annotations

from .base import Store
from .records import (
    APPROVED,
    AVAILABLE,
    BOOKED,
    BOOKING,
    CHECK_FAILED,
    EXPIRED,
    FAILED,
    HOLD_EXPIRED,
    OPEN_BOOKING_STATES,
    PENDING_APPROVAL,
    PREPARING,
    REJECTED,
    SOLD_OUT,
    UNKNOWN,
    WATCHING,
    Booking,
    Candidate,
    Want,
)
from .sqlite import SqliteStore

__all__ = [
    "APPROVED",
    "AVAILABLE",
    "BOOKED",
    "BOOKING",
    "CHECK_FAILED",
    "EXPIRED",
    "FAILED",
    "HOLD_EXPIRED",
    "OPEN_BOOKING_STATES",
    "PENDING_APPROVAL",
    "PREPARING",
    "REJECTED",
    "SOLD_OUT",
    "UNKNOWN",
    "WATCHING",
    "Booking",
    "Candidate",
    "SqliteStore",
    "Store",
    "Want",
]
