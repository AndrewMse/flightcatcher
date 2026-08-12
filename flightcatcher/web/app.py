"""Local web UI.

Binds to localhost by default. There is no authentication, because there is no
network exposure: if you put this on a public interface, put it behind
something that does auth. The approve endpoint spends money.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..client import WizzClient
from ..network import RouteNetwork
from ..search import SearchOptions, search
from ..settings import Settings
from ..store import PENDING_APPROVAL, Store

log = logging.getLogger(__name__)
UTC = timezone.utc

STATIC_DIR = Path(__file__).parent / "static"


class WantIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    origin: str = Field(min_length=2, max_length=40)
    destination: str = Field(min_length=2, max_length=40)
    date_from: date
    date_to: date
    max_stops: int = Field(default=1, ge=0, le=2)
    min_layover_min: int = Field(default=180, ge=30, le=2000)
    max_layover_min: int = Field(default=1200, ge=60, le=4000)
    max_detour: float = Field(default=2.2, ge=1.0, le=10.0)
    max_trip_hours: float = Field(default=30.0, ge=1.0, le=96.0)
    allow_ground_transfer: bool = False
    after_hour: int | None = Field(default=None, ge=0, le=23)
    before_hour: int | None = Field(default=None, ge=0, le=23)
    auto_request_booking: bool = False
    active: bool = True
    notes: str = ""


class WantPatch(BaseModel):
    active: bool | None = None
    auto_request_booking: bool | None = None
    name: str | None = None
    notes: str | None = None


class Decision(BaseModel):
    approved: bool


class AdHocSearch(BaseModel):
    origin: str
    destination: str
    date_from: date
    date_to: date
    max_stops: int = 1
    min_layover_min: int = 180
    max_trip_hours: float = 30.0
    allow_ground_transfer: bool = False


def create_app(store: Store, settings: Settings, watcher: Any | None = None) -> FastAPI:
    app = FastAPI(title="FlightCatcher", docs_url="/api/docs")
    app.state.network = None
    app.state.client = WizzClient()

    async def network() -> RouteNetwork:
        if app.state.network is None:
            raw = await asyncio.to_thread(app.state.client.route_map)
            app.state.network = RouteNetwork(raw)
        return app.state.network

    # --- status -------------------------------------------------------------

    @app.get("/api/status")
    async def get_status() -> dict[str, Any]:
        data: dict[str, Any] = {
            "now": datetime.now(UTC).isoformat(),
            "watcher": watcher.status() if watcher else {"running": False},
            "config_problems": settings.describe_problems(),
            "discord_enabled": settings.discord.enabled,
        }
        return data

    # --- wants --------------------------------------------------------------

    @app.get("/api/wants")
    async def list_wants() -> list[dict[str, Any]]:
        return [w.to_dict() for w in store.list_wants()]

    @app.post("/api/wants", status_code=201)
    async def create_want(payload: WantIn) -> dict[str, Any]:
        if payload.date_to < payload.date_from:
            raise HTTPException(400, "date_to is before date_from")

        net = await network()
        if not net.resolve(payload.origin):
            raise HTTPException(400, f"Unknown origin: {payload.origin!r}")
        if not net.resolve(payload.destination):
            raise HTTPException(400, f"Unknown destination: {payload.destination!r}")

        want_id = store.add_want(
            **payload.model_dump()
            | {
                "date_from": payload.date_from.isoformat(),
                "date_to": payload.date_to.isoformat(),
                "allow_ground_transfer": int(payload.allow_ground_transfer),
                "auto_request_booking": int(payload.auto_request_booking),
                "active": int(payload.active),
            }
        )
        store.log("want", f"Added want {payload.name!r}", want_id=want_id)
        want = store.get_want(want_id)
        return want.to_dict()

    @app.patch("/api/wants/{want_id}")
    async def patch_want(want_id: int, payload: WantPatch) -> dict[str, Any]:
        if store.get_want(want_id) is None:
            raise HTTPException(404, "No such want")
        fields = {k: v for k, v in payload.model_dump().items() if v is not None}
        for key in ("active", "auto_request_booking"):
            if key in fields:
                fields[key] = int(fields[key])
        store.update_want(want_id, **fields)
        return store.get_want(want_id).to_dict()

    @app.delete("/api/wants/{want_id}", status_code=204)
    async def delete_want(want_id: int) -> None:
        if store.get_want(want_id) is None:
            raise HTTPException(404, "No such want")
        store.delete_want(want_id)
        store.log("want", f"Deleted want {want_id}")

    @app.post("/api/wants/{want_id}/search")
    async def search_want_now(want_id: int) -> dict[str, Any]:
        want = store.get_want(want_id)
        if want is None:
            raise HTTPException(404, "No such want")
        if watcher is None:
            raise HTTPException(503, "Watcher is not running")
        found = await watcher._search_want(want, await network())
        return {"new_candidates": found}

    # --- candidates ---------------------------------------------------------

    @app.get("/api/candidates")
    async def list_candidates(
        want_id: int | None = None, status: str = "watching", limit: int = 200
    ) -> list[dict[str, Any]]:
        return [
            c.to_dict()
            for c in store.list_candidates(want_id=want_id, status=status, limit=limit)
        ]

    @app.get("/api/candidates/{candidate_id}")
    async def get_candidate(candidate_id: int) -> dict[str, Any]:
        candidate = store.get_candidate(candidate_id)
        if candidate is None:
            raise HTTPException(404, "No such candidate")
        return candidate.to_dict()

    # --- bookings -----------------------------------------------------------

    @app.get("/api/bookings")
    async def list_bookings(limit: int = 50) -> list[dict[str, Any]]:
        return [b.to_dict() for b in store.list_bookings(limit=limit)]

    @app.get("/api/bookings/pending")
    async def pending_bookings() -> list[dict[str, Any]]:
        out = []
        for booking in store.list_bookings(statuses=[PENDING_APPROVAL]):
            data = booking.to_dict()
            candidate = store.get_candidate(booking.candidate_id)
            data["candidate"] = candidate.to_dict() if candidate else None
            out.append(data)
        return out

    @app.post("/api/bookings/{booking_id}/decision")
    async def decide_booking(booking_id: int, payload: Decision) -> dict[str, Any]:
        if watcher is None:
            raise HTTPException(503, "Watcher is not running")
        booking = store.get_booking(booking_id)
        if booking is None:
            raise HTTPException(404, "No such booking")
        accepted = watcher.decide(booking_id, payload.approved, by="web")
        if not accepted:
            # Refused rather than queued: a decision that arrives after the held
            # seat expired must not book anything later.
            raise HTTPException(
                409,
                "This booking is no longer waiting for a decision — "
                "the hold expired or it was already decided.",
            )
        return {"ok": True, "approved": payload.approved}

    @app.get("/api/bookings/{booking_id}/screenshot")
    async def booking_screenshot(booking_id: int) -> FileResponse:
        booking = store.get_booking(booking_id)
        if booking is None or not booking.screenshot_path:
            raise HTTPException(404, "No screenshot for this booking")
        path = Path(booking.screenshot_path)
        if not path.exists():
            raise HTTPException(404, "Screenshot file is gone")
        return FileResponse(path, media_type="image/png")

    # --- lookups and ad-hoc search ------------------------------------------

    @app.get("/api/places")
    async def places(q: str) -> list[dict[str, Any]]:
        net = await network()
        codes = net.resolve(q)
        return [
            {
                "iata": code,
                "name": net.airport(code).name,
                "country": net.airport(code).country_name,
            }
            for code in codes[:40]
        ]

    @app.post("/api/search")
    async def ad_hoc_search(payload: AdHocSearch) -> dict[str, Any]:
        net = await network()
        origins = net.resolve(payload.origin)
        destinations = net.resolve(payload.destination)
        if not origins or not destinations:
            raise HTTPException(400, "Could not resolve origin or destination")

        opts = SearchOptions(
            date_from=payload.date_from,
            date_to=payload.date_to,
            max_stops=payload.max_stops,
            min_layover_min=payload.min_layover_min,
            max_trip_hours=payload.max_trip_hours,
            allow_ground_transfer=payload.allow_ground_transfer,
            limit=60,
        )
        result = await asyncio.to_thread(
            search, app.state.client, net, origins, destinations, opts
        )
        from ..cli import itinerary_to_dict

        return {
            "complete": result.is_complete,
            "failed_routes": [f"{a}→{b}" for a, b in result.failed_routes],
            "itineraries": [itinerary_to_dict(i) for i in result.itineraries],
        }

    # --- events -------------------------------------------------------------

    @app.get("/api/events")
    async def events(limit: int = 80, since_id: int = 0) -> list[dict[str, Any]]:
        return store.list_events(limit=limit, since_id=since_id)

    @app.get("/api/stream")
    async def stream(request: Request) -> StreamingResponse:
        """Server-sent events: new activity plus a periodic status heartbeat."""

        async def generate():
            last_id = 0
            rows = store.list_events(limit=1)
            if rows:
                last_id = rows[0]["id"]
            while True:
                if await request.is_disconnected():
                    break
                fresh = store.list_events(limit=50, since_id=last_id)
                if fresh:
                    last_id = max(e["id"] for e in fresh)
                    for event in reversed(fresh):
                        yield f"event: activity\ndata: {json.dumps(event)}\n\n"
                status = {
                    "watcher": watcher.status() if watcher else {"running": False},
                    "now": datetime.now(UTC).isoformat(),
                }
                yield f"event: status\ndata: {json.dumps(status)}\n\n"
                await asyncio.sleep(3)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # --- frontend -----------------------------------------------------------

    if STATIC_DIR.exists():
        app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

    return app
