"""What the AWS backend would cost per month, from measured request counts.

An estimate, not a bill: request counts come from a benchmark against moto,
and Lambda duration is projected from the real politeness limit rather than
measured on AWS. Prices are eu-central-1 list prices from the AWS Price List
API (offer files published 2026-09-11 to 2026-10-01).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

PRICES = {
    "dynamodb_write_unit": 0.7625e-6,      # on-demand, per write request unit
    "dynamodb_read_unit": 0.1525e-6,       # on-demand, per read request unit
    "lambda_request": 0.20e-6,             # Arm
    "lambda_gb_second_arm": 0.0000133334,  # Arm, first tier
    "sqs_request": 0.40e-6,                # standard queue
    "logs_ingest_gb": 0.63,                # CloudWatch Logs, standard class
    "alarm_month": 0.10,                   # standard resolution
    "custom_metric_month": 0.30,
}

# Always-free monthly allowances (not the 12-month new-account offers).
ALWAYS_FREE = {
    "lambda_requests": 1_000_000,
    "lambda_gb_seconds": 400_000,
    "sqs_requests": 1_000_000,
    "logs_gb": 5,
    "alarms": 10,
    "custom_metrics": 10,
}

ALARMS = 5
CUSTOM_METRICS = 1  # BookerHeartbeatAgeSeconds
# Lambda's SQS event source long-polls with about five connections, each
# returning every 20 s when idle: SQS bills those receives too.
EVENT_SOURCE_POLLS_PER_MONTH = 5 * 3 * 60 * 24 * 30


@dataclass
class UsageCounts:
    """What one sweep of every want costs, in billable units."""

    jobs: int
    sqs_requests: int
    dynamo_reads: float
    dynamo_writes: float
    worker_seconds: float  # billed Lambda duration across all workers
    log_bytes: int


@dataclass
class CostBreakdown:
    lambda_requests: float
    lambda_compute: float
    sqs: float
    dynamodb: float
    logs: float
    monitoring: float
    usage: dict[str, float]

    @property
    def total(self) -> float:
        return (
            self.lambda_requests + self.lambda_compute + self.sqs
            + self.dynamodb + self.logs + self.monitoring
        )

    def to_dict(self) -> dict[str, object]:
        return {**asdict(self), "total": self.total}


def monthly_cost(
    per_sweep: UsageCounts,
    sweeps_per_month: int,
    free_tier: bool,
    *,
    memory_gb: float = 0.25,
    planner_seconds: float = 1.0,
    include_fixed: bool = True,
) -> CostBreakdown:
    n = sweeps_per_month
    requests = (per_sweep.jobs + 1) * n  # +1: the planner run that queued them
    gb_seconds = (per_sweep.worker_seconds + planner_seconds) * memory_gb * n
    sqs_requests = per_sweep.sqs_requests * n + (EVENT_SOURCE_POLLS_PER_MONTH if include_fixed else 0)
    logs_gb = per_sweep.log_bytes * n / 1024**3
    alarms = ALARMS if include_fixed else 0
    metrics = CUSTOM_METRICS if include_fixed else 0

    def billable(amount: float, allowance_key: str) -> float:
        return max(0.0, amount - ALWAYS_FREE[allowance_key]) if free_tier else amount

    return CostBreakdown(
        lambda_requests=billable(requests, "lambda_requests") * PRICES["lambda_request"],
        lambda_compute=billable(gb_seconds, "lambda_gb_seconds") * PRICES["lambda_gb_second_arm"],
        sqs=billable(sqs_requests, "sqs_requests") * PRICES["sqs_request"],
        dynamodb=(
            per_sweep.dynamo_reads * PRICES["dynamodb_read_unit"]
            + per_sweep.dynamo_writes * PRICES["dynamodb_write_unit"]
        ) * n,
        logs=billable(logs_gb, "logs_gb") * PRICES["logs_ingest_gb"],
        monitoring=(
            billable(alarms, "alarms") * PRICES["alarm_month"]
            + billable(metrics, "custom_metrics") * PRICES["custom_metric_month"]
        ),
        usage={
            "lambda_requests": requests,
            "lambda_gb_seconds": gb_seconds,
            "sqs_requests": sqs_requests,
            "dynamodb_read_units": per_sweep.dynamo_reads * n,
            "dynamodb_write_units": per_sweep.dynamo_writes * n,
            "logs_gb": logs_gb,
        },
    )
