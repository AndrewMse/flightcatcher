"""Core data types."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass(frozen=True)
class Airport:
    iata: str
    name: str
    country_code: str
    country_name: str
    lat: float
    lon: float
    mac: str | None = None  # metropolitan area code, e.g. BUH for OTP/BBU

    @property
    def label(self) -> str:
        return f"{self.iata} ({self.name})"


@dataclass(frozen=True)
class Edge:
    """A route Wizz Air operates between two stations."""

    origin: str
    destination: str
    distance_km: float
    starts: datetime | None = None  # route not yet flying before this date


@dataclass(frozen=True)
class Departure:
    """One scheduled departure, as reported by the timetable endpoint.

    ``arrives_utc`` is estimated, not published -- see config's duration model.
    """

    origin: str
    destination: str
    departs_local: datetime
    departs_utc: datetime
    duration_min: int
    price_amount: float | None
    price_currency: str | None

    @property
    def arrives_utc(self) -> datetime:
        return self.departs_utc + timedelta(minutes=self.duration_min)


@dataclass(frozen=True)
class Connection:
    """The gap between two consecutive legs."""

    layover_min: int
    ground_transfer: bool  # arrival and next departure are different airports
    from_station: str
    to_station: str


@dataclass
class BookingWindow:
    """When a whole itinerary can actually be bought on Multipass.

    Every leg is a separate booking, and each leg's window opens 72h before
    *that leg* departs. The itinerary is only safely committable once the last
    leg has opened -- see ``staggered_hours``.
    """

    opens_utc: datetime  # max over legs of (departure - 72h)
    closes_utc: datetime  # min over legs of (departure - cutoff)
    first_leg_opens_utc: datetime
    status: str  # "too_early" | "open" | "closed"

    @property
    def staggered_hours(self) -> float:
        """How long leg 1 is bookable before the itinerary as a whole is.

        Non-zero means a race: leg 1's seats can vanish while you are still
        waiting for the later legs to unlock.
        """
        delta = self.opens_utc - self.first_leg_opens_utc
        return round(delta.total_seconds() / 3600, 1)

    @property
    def is_viable(self) -> bool:
        return self.status != "closed" and self.closes_utc > self.opens_utc


@dataclass
class Itinerary:
    legs: list[Departure]
    connections: list[Connection] = field(default_factory=list)
    window: BookingWindow | None = None

    @property
    def origin(self) -> str:
        return self.legs[0].origin

    @property
    def destination(self) -> str:
        return self.legs[-1].destination

    @property
    def stops(self) -> int:
        return len(self.legs) - 1

    @property
    def path(self) -> list[str]:
        out = [self.legs[0].origin]
        for leg in self.legs:
            out.append(leg.destination)
        return out

    @property
    def departs_utc(self) -> datetime:
        return self.legs[0].departs_utc

    @property
    def arrives_utc(self) -> datetime:
        return self.legs[-1].arrives_utc

    @property
    def total_minutes(self) -> int:
        return int((self.arrives_utc - self.departs_utc).total_seconds() // 60)

    @property
    def has_ground_transfer(self) -> bool:
        return any(c.ground_transfer for c in self.connections)
