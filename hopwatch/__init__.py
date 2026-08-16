"""Hopwatch — Wizz Air Multipass itinerary finder."""

from __future__ import annotations

__version__ = "0.3.0"

from .client import WizzClient, WizzError
from .models import Airport, BookingWindow, Connection, Departure, Edge, Itinerary
from .network import RouteNetwork
from .search import SearchOptions, SearchResult, booking_window, search

__all__ = [
    "Airport",
    "BookingWindow",
    "Connection",
    "Departure",
    "Edge",
    "Itinerary",
    "RouteNetwork",
    "SearchOptions",
    "SearchResult",
    "WizzClient",
    "WizzError",
    "booking_window",
    "search",
]
