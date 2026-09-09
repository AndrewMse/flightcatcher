"""The assembled service: scheduler and in-process worker, end to end."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

from hopwatch.network import RouteNetwork
from hopwatch.service import Service
from hopwatch.settings import Settings
from hopwatch.store import JOB_DONE, SqliteStore
from hopwatch.sweep import SweepResult

from .conftest import build_map


async def test_service_sweeps_wants_through_the_queue(tmp_path, monkeypatch) -> None:
    swept: list[int] = []

    def fake_sweep(store, client, network, want, today=None):
        swept.append(want.id)
        return SweepResult(itineraries=0, new_candidates=0, complete=True)

    monkeypatch.setattr("hopwatch.jobs.worker.sweep_want", fake_sweep)
    monkeypatch.setattr(
        "hopwatch.service.network_loader", lambda client: lambda: RouteNetwork(build_map())
    )

    settings = Settings.load(tmp_path / "missing.toml")
    settings.database = tmp_path / "svc.db"
    settings.web.enabled = False
    settings.browser.keepalive_min = 0

    seed = SqliteStore(settings.database)
    want_id = seed.add_want(
        name="Home", origin="OTP", destination="EIN", date_from=date.today().isoformat(),
        date_to=(date.today() + timedelta(days=3)).isoformat(), max_stops=1,
        min_layover_min=180, max_layover_min=1200, max_detour=2.2, max_trip_hours=30.0,
        allow_ground_transfer=0, after_hour=None, before_hour=None,
        auto_request_booking=0, active=1, notes="",
    )

    service = Service(settings)
    running = asyncio.create_task(service.run())
    try:
        for _ in range(100):
            jobs = seed.list_jobs()
            if jobs and jobs[0].status == JOB_DONE:
                break
            await asyncio.sleep(0.1)
    finally:
        service._stopping.set()
        await asyncio.wait_for(running, timeout=10)

    assert swept == [want_id]
    assert [j.status for j in seed.list_jobs()] == [JOB_DONE]
    seed.close()
