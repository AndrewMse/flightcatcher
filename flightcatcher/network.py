"""The Wizz Air route network as a graph.

Built from ``asset/map``, which currently yields 196 stations and ~3300
directed edges. Two wrinkles in that payload drive most of the code here:

* **Metro area codes.** OTP and BBU are both ``mac: "BUH"``; WAW and WMI are
  both ``WSW``. Some MACs additionally appear as their own pseudo-station in
  the city list (PAR, MIL, ROM, LON, STO...). Connections may point at either a
  real station or a MAC, so every target gets expanded to real stations.

* **Station names carry stray CRLFs** ("Bucharest Otopeni\\r\\n").
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import date, datetime
from typing import Any, Iterable, Sequence

from . import config
from .models import Airport, Edge

log = logging.getLogger(__name__)

EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def estimate_duration_min(distance_km: float) -> int:
    """Block time for a sector, since the timetable endpoint omits arrivals."""
    cruise = (distance_km * config.ROUTE_INEFFICIENCY) / config.CRUISE_KMH * 60.0
    return int(round(config.FIXED_OVERHEAD_MIN + cruise))


def _parse_dt(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class RouteNetwork:
    def __init__(self, raw_map: dict[str, Any]) -> None:
        self.airports: dict[str, Airport] = {}
        self._pseudo: set[str] = set()  # MAC entries masquerading as stations
        self._mac_members: dict[str, list[str]] = defaultdict(list)
        self.edges: dict[tuple[str, str], Edge] = {}
        self._out: dict[str, set[str]] = defaultdict(set)

        self._load(raw_map)

    # --- construction -------------------------------------------------------

    def _load(self, raw_map: dict[str, Any]) -> None:
        cities = raw_map.get("cities") or []
        if not cities:
            raise ValueError("route map contained no cities")

        for city in cities:
            iata = (city.get("iata") or "").strip().upper()
            if not iata:
                continue
            mac = (city.get("mac") or "").strip().upper() or None
            airport = Airport(
                iata=iata,
                name=(city.get("shortName") or iata).strip(),
                country_code=(city.get("countryCode") or "").strip().upper(),
                country_name=(city.get("countryName") or "").strip(),
                lat=float(city.get("latitude") or 0.0),
                lon=float(city.get("longitude") or 0.0),
                mac=mac,
            )
            self.airports[iata] = airport
            if mac == iata:
                self._pseudo.add(iata)
            elif mac:
                self._mac_members[mac].append(iata)

        # A pseudo-station is a grouping, never a place you can fly to.
        for iata in self._pseudo:
            self._mac_members.setdefault(iata, [])

        for city in cities:
            origin = (city.get("iata") or "").strip().upper()
            if origin in self._pseudo or origin not in self.airports:
                continue
            for conn in city.get("connections") or []:
                for dest in self._expand(conn.get("iata")):
                    if dest == origin:
                        continue
                    self._add_edge(origin, dest, _parse_dt(conn.get("operationStartDate")))

        log.debug(
            "network: %d stations, %d edges",
            len(self.stations),
            len(self.edges),
        )

    def _add_edge(self, origin: str, dest: str, starts: datetime | None) -> None:
        key = (origin, dest)
        existing = self.edges.get(key)
        # Keep the earliest start date when a MAC expands to several targets.
        if existing is not None:
            if starts and existing.starts and starts >= existing.starts:
                return
            if starts is None:
                return
        a, b = self.airports[origin], self.airports[dest]
        self.edges[key] = Edge(
            origin=origin,
            destination=dest,
            distance_km=haversine_km(a.lat, a.lon, b.lat, b.lon),
            starts=starts,
        )
        self._out[origin].add(dest)

    def _expand(self, code: Any) -> list[str]:
        """Turn a connection target into the real stations it stands for."""
        if not code:
            return []
        key = str(code).strip().upper()
        if key in self.airports and key not in self._pseudo:
            return [key]
        members = self._mac_members.get(key)
        if members:
            return list(members)
        return []

    # --- accessors ----------------------------------------------------------

    @property
    def stations(self) -> list[str]:
        return sorted(i for i in self.airports if i not in self._pseudo)

    def airport(self, iata: str) -> Airport | None:
        return self.airports.get(iata.strip().upper())

    def distance_km(self, origin: str, dest: str) -> float:
        a, b = self.airports.get(origin), self.airports.get(dest)
        if not a or not b:
            return 0.0
        return haversine_km(a.lat, a.lon, b.lat, b.lon)

    def duration_min(self, origin: str, dest: str) -> int:
        return estimate_duration_min(self.distance_km(origin, dest))

    def same_metro(self, a: str, b: str) -> bool:
        if a == b:
            return True
        aa, bb = self.airports.get(a), self.airports.get(b)
        if not aa or not bb:
            return False
        return bool(aa.mac and aa.mac == bb.mac)

    def destinations_from(self, origin: str) -> set[str]:
        return set(self._out.get(origin, ()))

    def has_route(self, origin: str, dest: str) -> bool:
        return (origin, dest) in self.edges

    # --- place resolution ---------------------------------------------------

    def resolve(self, token: str) -> list[str]:
        """Turn user input into a set of real stations.

        Accepts an IATA code (``EIN``), a metro code (``BUH`` -> OTP+BBU), a
        city name (``Bucharest``), a country code (``NL``) or a country name.
        """
        key = token.strip().upper()
        if not key:
            return []

        if key in self.airports and key not in self._pseudo:
            return [key]

        members = self._mac_members.get(key)
        if members:
            return sorted(members)

        if len(key) == 2:
            by_country = [
                i for i in self.stations if self.airports[i].country_code == key
            ]
            if by_country:
                return sorted(by_country)

        needle = token.strip().lower()
        by_city = [
            i for i in self.stations if needle in self.airports[i].name.lower()
        ]
        if by_city:
            # A city name should pull in the whole metro area, not just the
            # airports whose names happen to embed it.
            expanded: set[str] = set(by_city)
            for iata in by_city:
                mac = self.airports[iata].mac
                if mac:
                    expanded.update(self._mac_members.get(mac, []))
            return sorted(expanded)

        by_country_name = [
            i for i in self.stations if needle in self.airports[i].country_name.lower()
        ]
        return sorted(by_country_name)

    # --- path finding -------------------------------------------------------

    def find_paths(
        self,
        origins: Sequence[str],
        destinations: Sequence[str],
        max_stops: int = config.DEFAULT_MAX_STOPS,
        max_detour: float = config.DEFAULT_MAX_DETOUR,
        on_or_after: date | None = None,
    ) -> list[list[str]]:
        """All station paths from any origin to any destination.

        Pruned by detour ratio so a Bucharest-to-Eindhoven search does not
        offer to route you via Yerevan.
        """
        origin_set = {o for o in origins if o in self.airports}
        dest_set = {d for d in destinations if d in self.airports}
        if not origin_set or not dest_set:
            return []

        # Metro areas of the endpoints -- never connect through your own city.
        endpoint_metros = {
            self.airports[i].mac or i for i in origin_set | dest_set
        }

        def usable(origin: str, dest: str) -> bool:
            edge = self.edges.get((origin, dest))
            if edge is None:
                return False
            if on_or_after and edge.starts and edge.starts.date() > on_or_after:
                return False
            return True

        paths: list[list[str]] = []
        seen: set[tuple[str, ...]] = set()

        for origin in sorted(origin_set):
            direct_refs = [self.distance_km(origin, d) for d in dest_set]
            budget = max(direct_refs) * max_detour + 200.0

            def walk(path: list[str], travelled: float) -> None:
                current = path[-1]
                for nxt in sorted(self._out.get(current, ())):
                    if nxt in path or not usable(current, nxt):
                        continue
                    step = self.edges[(current, nxt)].distance_km
                    if travelled + step > budget:
                        continue

                    if nxt in dest_set:
                        key = tuple(path + [nxt])
                        if key not in seen:
                            seen.add(key)
                            paths.append(list(key))
                        continue

                    # Intermediate stop: must be a genuine elsewhere, and we
                    # must still have a stop left to spend.
                    if len(path) - 1 >= max_stops:
                        continue
                    if (self.airports[nxt].mac or nxt) in endpoint_metros:
                        continue
                    walk(path + [nxt], travelled + step)

            walk([origin], 0.0)

        paths.sort(key=lambda p: (len(p), sum(
            self.edges[(p[i], p[i + 1])].distance_km for i in range(len(p) - 1)
        )))
        return paths

    def path_distance_km(self, path: Iterable[str]) -> float:
        stations = list(path)
        return sum(
            self.distance_km(stations[i], stations[i + 1])
            for i in range(len(stations) - 1)
        )
