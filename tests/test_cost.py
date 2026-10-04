"""The monthly AWS cost model used in the README's results."""

from __future__ import annotations

import pytest

from bench.cost import PRICES, UsageCounts, monthly_cost

SWEEP = UsageCounts(
    jobs=10, sqs_requests=30, dynamo_reads=400.0, dynamo_writes=250.0,
    worker_seconds=120.0, log_bytes=40_000,
)


def test_prices_are_the_published_eu_central_1_list_prices() -> None:
    assert PRICES["dynamodb_write_unit"] == pytest.approx(0.7625e-6)
    assert PRICES["dynamodb_read_unit"] == pytest.approx(0.1525e-6)
    assert PRICES["lambda_gb_second_arm"] == pytest.approx(0.0000133334)
    assert PRICES["sqs_request"] == pytest.approx(0.40e-6)


def test_usage_driven_costs_scale_linearly_with_sweeps() -> None:
    one = monthly_cost(SWEEP, sweeps_per_month=1000, free_tier=False)
    two = monthly_cost(SWEEP, sweeps_per_month=2000, free_tier=False)
    assert two.dynamodb == pytest.approx(2 * one.dynamodb)
    assert two.lambda_compute == pytest.approx(2 * one.lambda_compute)
    # Alarms and the custom metric cost the same however often we sweep.
    assert two.monitoring == pytest.approx(one.monitoring)


def test_known_values() -> None:
    usage = UsageCounts(jobs=0, sqs_requests=0, dynamo_reads=0, dynamo_writes=1_000_000,
                        worker_seconds=0, log_bytes=0)
    cost = monthly_cost(usage, sweeps_per_month=1, free_tier=False, include_fixed=False)
    assert cost.dynamodb == pytest.approx(0.7625)
    assert cost.total == pytest.approx(0.7625, abs=1e-4)  # plus one planner run


def test_free_tier_zeroes_small_usage() -> None:
    cost = monthly_cost(SWEEP, sweeps_per_month=2880, free_tier=True)
    assert cost.lambda_requests == 0
    assert cost.lambda_compute == 0
    assert cost.sqs == 0
    assert cost.logs == 0
    assert cost.monitoring == 0
    # DynamoDB on-demand requests have no always-free allowance.
    assert cost.dynamodb > 0


def test_free_tier_only_covers_the_allowance() -> None:
    heavy = UsageCounts(jobs=10, sqs_requests=30, dynamo_reads=0, dynamo_writes=0,
                        worker_seconds=600.0, log_bytes=0)
    cost = monthly_cost(heavy, sweeps_per_month=2880, free_tier=True, include_fixed=False)
    gb_seconds = 600.0 * 0.25 * 2880 + 2880 * 1.0 * 0.25  # workers + planner
    expected = (gb_seconds - 400_000) * PRICES["lambda_gb_second_arm"]
    assert cost.lambda_compute == pytest.approx(expected)


def test_breakdown_serialises() -> None:
    data = monthly_cost(SWEEP, sweeps_per_month=2880, free_tier=False).to_dict()
    assert set(data) >= {"lambda_requests", "lambda_compute", "sqs", "dynamodb", "logs",
                         "monitoring", "total", "usage"}
    assert data["usage"]["lambda_gb_seconds"] > 0
