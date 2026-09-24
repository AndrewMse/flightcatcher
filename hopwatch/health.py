"""Pipeline health: the few signals that mean a flight is about to be missed.

Searches that silently stop are the dangerous failure here. Nothing crashes
and nothing errors; the watch list just stops changing while a 72h window
opens and closes unnoticed. So each signal is phrased as "has the pipeline
stopped doing its job", not "did something throw".

Locally the booker evaluates these and alerts Discord. On AWS, CloudWatch
alarms watch the same things (see ``infra/``) and the booker still checks too.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .jobs.queue import JobQueue
from .store import JOB_DONE, JOB_QUEUED, JOB_RETRYING, Store

FAILURE_WINDOW = timedelta(hours=1)
FAILURE_MIN_JOBS = 4
FAILURE_RATIO = 0.5
BOOKER_SILENT_AFTER = timedelta(minutes=15)


@dataclass
class Signal:
    name: str
    firing: bool
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "firing": self.firing, "detail": self.detail}


def evaluate(
    store: Store,
    queue: JobQueue,
    now: datetime,
    interval_min: int,
    include_booker: bool = False,
) -> list[Signal]:
    signals = [_dead_letters(queue), _queue_stalled(store, now, interval_min), _job_failures(store, now)]
    if include_booker:
        signals.append(_booker_silent(store, now))
    return signals


def _dead_letters(queue: JobQueue) -> Signal:
    dead = queue.depth().dead
    return Signal(
        "dead_letters",
        dead > 0,
        f"{dead} search job(s) dead-lettered after repeated failures" if dead else "",
    )


def _queue_stalled(store: Store, now: datetime, interval_min: int) -> Signal:
    # Age from creation, not last update: re-sending a lost job touches it,
    # and that must not make a stuck job look fresh.
    limit = now - timedelta(minutes=2 * interval_min)
    waiting = [
        job
        for status in (JOB_QUEUED, JOB_RETRYING)
        for job in store.list_jobs(status=status, limit=500)
        if job.created_at < limit
    ]
    if not waiting:
        return Signal("queue_stalled", False, "")
    oldest = min(waiting, key=lambda j: j.created_at)
    minutes = int((now - oldest.created_at).total_seconds() // 60)
    return Signal(
        "queue_stalled",
        True,
        f"{len(waiting)} job(s) waiting; {oldest.key} for {minutes} min. Are any workers running?",
    )


def _job_failures(store: Store, now: datetime) -> Signal:
    outcomes = store.job_outcomes_since(now - FAILURE_WINDOW)
    total = sum(outcomes.values())
    bad = total - outcomes.get(JOB_DONE, 0)
    firing = total >= FAILURE_MIN_JOBS and bad / total > FAILURE_RATIO
    return Signal(
        "job_failures",
        firing,
        f"{bad} of {total} search jobs in the last hour did not finish" if firing else "",
    )


def _booker_silent(store: Store, now: datetime) -> Signal:
    last = store.heartbeats().get("booker")
    if last is None:
        return Signal("booker_silent", True, "the booker has never reported in")
    silent = now - last
    if silent <= BOOKER_SILENT_AFTER:
        return Signal("booker_silent", False, "")
    return Signal(
        "booker_silent",
        True,
        f"no word from the booker for {int(silent.total_seconds() // 60)} min — "
        f"nothing can be booked until it is back",
    )


class AlertTracker:
    """Turns a stream of evaluations into "started failing" and "recovered".

    An alert per evaluation would be one every five minutes for as long as a
    problem lasts, which is how people learn to ignore alerts.
    """

    def __init__(self) -> None:
        self._firing: set[str] = set()

    def update(self, signals: list[Signal]) -> tuple[list[Signal], list[Signal]]:
        started = [s for s in signals if s.firing and s.name not in self._firing]
        recovered = [s for s in signals if not s.firing and s.name in self._firing]
        self._firing = {s.name for s in signals if s.firing}
        return started, recovered
