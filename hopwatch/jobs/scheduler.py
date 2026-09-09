"""Turning "sweep every want every N minutes" into queued jobs.

Every job is keyed by want and time slot, and the key is claimed in the store
before anything is sent. So a scheduler that fires twice, two schedulers
running at once (the booker and EventBridge), or a double-clicked "Sweep now"
all produce one job, not several.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..store import JOB_QUEUED, Store
from .queue import JobQueue

KIND = "search_want"


def slot_key(want_id: int, now: datetime, interval_min: int) -> str:
    slot = int(now.timestamp()) // (interval_min * 60)
    return f"search:{want_id}:{slot}"


def manual_key(want_id: int, now: datetime) -> str:
    return f"manual:{want_id}:{int(now.timestamp()) // 60}"


def _enqueue(store: Store, queue: JobQueue, key: str, want_id: int) -> bool:
    if not store.create_job(key, KIND, want_id):
        return False
    queue.send({"job": key})
    return True


def enqueue_sweep(store: Store, queue: JobQueue, now: datetime, interval_min: int) -> int:
    """Queue one sweep per active want for the current slot. Returns jobs created."""
    return sum(
        _enqueue(store, queue, slot_key(want.id, now, interval_min), want.id)
        for want in store.list_wants(active_only=True)
    )


def enqueue_manual(
    store: Store, queue: JobQueue, want_id: int, now: datetime
) -> tuple[str, bool]:
    key = manual_key(want_id, now)
    return key, _enqueue(store, queue, key, want_id)


def resend_stale(store: Store, queue: JobQueue, now: datetime, older_than_s: int) -> int:
    """Re-send jobs that were recorded but never picked up.

    Covers a crash between creating the job and sending its message. Sending
    twice is harmless -- the worker skips a job that is already done -- and
    touching the job means it is re-sent at most once per period.
    """
    stale = store.stale_jobs(
        older_than=now - timedelta(seconds=older_than_s), statuses=(JOB_QUEUED,)
    )
    for job in stale:
        queue.send({"job": job.key})
        store.touch_job(job.key, now)
    return len(stale)
