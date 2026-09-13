"""DynamoDB table layout, defined once.

The CDK stack and ``create_tables`` (used by tests, the benchmark and a manual
bootstrap) both read this, so the tables the code expects and the tables the
infrastructure creates cannot drift apart.

Every table is on-demand: at a few thousand writes a day, paying per request
costs cents, where provisioned capacity would cost dollars to sit idle.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import boto3
from botocore.exceptions import ClientError


@dataclass(frozen=True)
class GsiSpec:
    name: str
    pk: tuple[str, str]
    sk: tuple[str, str]


@dataclass(frozen=True)
class TableSpec:
    name: str
    pk: tuple[str, str]  # (attribute, "S" | "N")
    sk: tuple[str, str] | None = None
    gsis: tuple[GsiSpec, ...] = field(default_factory=tuple)
    ttl: str | None = None


TABLES: dict[str, TableSpec] = {
    spec.name: spec
    for spec in (
        TableSpec("wants", pk=("id", "N")),
        TableSpec(
            "candidates",
            pk=("id", "N"),
            # status + "window_opens#departs": candidates in unlock order, and
            # the open-window range query that decides what gets checked.
            gsis=(GsiSpec("by_status", pk=("status", "S"), sk=("window_key", "S")),),
        ),
        TableSpec("checks", pk=("candidate_id", "N"), sk=("checked_key", "S")),
        TableSpec("bookings", pk=("id", "N")),
        TableSpec("events", pk=("pk", "S"), sk=("id", "N"), ttl="expires_at"),
        TableSpec(
            "jobs",
            pk=("key", "S"),
            gsis=(GsiSpec("by_status", pk=("status", "S"), sk=("created_at", "S")),),
            ttl="expires_at",
        ),
        TableSpec("search_runs", pk=("want_id", "N"), sk=("id", "S"), ttl="expires_at"),
        # Counters, uniqueness markers, heartbeats, the rate limit and the
        # response cache: small string-keyed items that need no index.
        TableSpec("meta", pk=("pk", "S"), ttl="expires_at"),
    )
}


def table_name(prefix: str, name: str) -> str:
    return f"{prefix}-{name}"


def create_tables(prefix: str, region: str, endpoint_url: str | None = None) -> None:
    """Create every table if it does not exist yet. Safe to run repeatedly."""
    client = boto3.client("dynamodb", region_name=region, endpoint_url=endpoint_url)
    for spec in TABLES.values():
        name = table_name(prefix, spec.name)
        keys = [spec.pk] + ([spec.sk] if spec.sk else [])
        for gsi in spec.gsis:
            keys += [gsi.pk, gsi.sk]
        definitions = {attr: kind for attr, kind in keys}
        schema = [{"AttributeName": spec.pk[0], "KeyType": "HASH"}]
        if spec.sk:
            schema.append({"AttributeName": spec.sk[0], "KeyType": "RANGE"})
        kwargs = {
            "TableName": name,
            "BillingMode": "PAY_PER_REQUEST",
            "AttributeDefinitions": [
                {"AttributeName": attr, "AttributeType": kind}
                for attr, kind in definitions.items()
            ],
            "KeySchema": schema,
        }
        if spec.gsis:
            kwargs["GlobalSecondaryIndexes"] = [
                {
                    "IndexName": gsi.name,
                    "KeySchema": [
                        {"AttributeName": gsi.pk[0], "KeyType": "HASH"},
                        {"AttributeName": gsi.sk[0], "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
                for gsi in spec.gsis
            ]
        try:
            client.create_table(**kwargs)
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ResourceInUseException":
                raise
            continue
        client.get_waiter("table_exists").wait(TableName=name)
        if spec.ttl:
            client.update_time_to_live(
                TableName=name,
                TimeToLiveSpecification={"Enabled": True, "AttributeName": spec.ttl},
            )
