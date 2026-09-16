"""Lambda entry points: the planner (on a schedule) and the search worker (on SQS).

Both are thin: they build the AWS backend from environment variables once per
container, then call exactly the code the local service runs. A sweep on
Lambda and a sweep on a laptop are the same function.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from ..backend import Backend, make_client, open_backend
from ..jobs.queue import Message
from ..jobs.scheduler import enqueue_sweep, resend_stale
from ..jobs.worker import JobRunner, network_loader
from ..settings import Settings

log = logging.getLogger(__name__)
UTC = timezone.utc

METRIC_NAMESPACE = "Hopwatch"

# Built on first use and kept for the life of the container, so a warm
# invocation reuses its connections, route network and Wizz session.
_state: dict[str, Any] = {}


def _settings() -> Settings:
    if "settings" not in _state:
        _state["settings"] = Settings.from_env()
    return _state["settings"]


def backend() -> Backend:
    if "backend" not in _state:
        _state["backend"] = open_backend(_settings())
    return _state["backend"]


def _runner() -> JobRunner:
    if "runner" not in _state:
        settings = _settings()
        client = make_client(settings, backend())
        _state["runner"] = JobRunner(
            backend().store,
            client,
            network=network_loader(client),
            worker_id=f"lambda-{uuid.uuid4().hex[:8]}",
            lease_s=settings.queue.visibility_s,
            max_receives=settings.queue.max_receives,
        )
    return _state["runner"]


def worker_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """SQS event source, batch size 1, with ReportBatchItemFailures.

    A retry outcome first moves the message's visibility to the backoff delay,
    then reports it as failed so SQS keeps it rather than deleting it.
    """
    runner = _runner()
    queue = backend().queue
    failures: list[dict[str, str]] = []
    for record in event.get("Records", []):
        receive_count = int(record.get("attributes", {}).get("ApproximateReceiveCount", 1))
        message = Message(
            id=record["messageId"],
            body={},
            receive_count=receive_count,
            receipt=record["receiptHandle"],
            sent_at=datetime.now(UTC),
        )
        try:
            outcome = runner.handle(record.get("body", ""), receive_count)
        except Exception:  # noqa: BLE001
            log.exception("could not handle message %s", record["messageId"])
            failures.append({"itemIdentifier": record["messageId"]})
            continue
        if outcome.action == "retry":
            try:
                queue.retry_later(message, outcome.delay_s)
            except Exception:  # noqa: BLE001 - the default visibility still applies
                log.warning("could not delay message %s", record["messageId"])
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}


def planner_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """EventBridge Scheduler, every search interval: queue this slot's sweeps."""
    settings = _settings()
    store, queue = backend().store, backend().queue
    now = datetime.now(UTC)
    interval = settings.watcher.search_interval_min

    enqueued = enqueue_sweep(store, queue, now, interval)
    resent = resend_stale(store, queue, now, older_than_s=interval * 60)
    store.expire_stale_candidates(now)

    booker = store.heartbeats().get("booker")
    age = (now - booker).total_seconds() if booker else None
    if age is not None:
        # Embedded Metric Format: CloudWatch turns this log line into a metric
        # with no API call. No line at all when the booker has never reported,
        # so the alarm sees missing data, which it treats as breaching.
        print(json.dumps({
            "_aws": {
                "Timestamp": int(time.time() * 1000),
                "CloudWatchMetrics": [{
                    "Namespace": METRIC_NAMESPACE,
                    "Dimensions": [[]],
                    "Metrics": [{"Name": "BookerHeartbeatAgeSeconds", "Unit": "Seconds"}],
                }],
            },
            "BookerHeartbeatAgeSeconds": age,
        }))
    log.info("planned %d sweep(s), re-sent %d", enqueued, resent)
    return {"enqueued": enqueued, "resent": resent, "booker_heartbeat_age_s": age}
