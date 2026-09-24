"""The Wizz politeness budget, shared by every Lambda worker through DynamoDB.

Same algorithm as the SQLite limiter: each caller reserves the next free slot
with a conditional write, then sleeps until it arrives. A lost race simply
re-reads and tries again, so concurrent workers end up strictly spaced.
"""

from __future__ import annotations

import time
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

from .schema import table_name


class DynamoRateLimiter:
    def __init__(
        self,
        prefix: str,
        region: str,
        endpoint_url: str | None = None,
        min_interval: float = 1.5,
        name: str = "wizz",
    ) -> None:
        self.min_interval = min_interval
        self._key = {"pk": f"ratelimit#{name}"}
        self._table = boto3.resource(
            "dynamodb", region_name=region, endpoint_url=endpoint_url
        ).Table(table_name(prefix, "meta"))

    def close(self) -> None:
        return None

    def wait(self) -> float:
        if self.min_interval <= 0:
            return time.time()
        slot = self._reserve()
        delay = slot - time.time()
        if delay > 0:
            time.sleep(delay)
        return slot

    def _reserve(self) -> float:
        while True:
            item = self._table.get_item(Key=self._key, ConsistentRead=True).get("Item")
            previous = item.get("next_slot") if item else None
            slot = max(time.time(), float(previous) if previous is not None else 0.0)
            new = Decimal(repr(slot + self.min_interval))
            try:
                if previous is None:
                    self._table.put_item(
                        Item={**self._key, "next_slot": new},
                        ConditionExpression="attribute_not_exists(next_slot)",
                    )
                else:
                    self._table.update_item(
                        Key=self._key,
                        UpdateExpression="SET next_slot = :new",
                        ConditionExpression="next_slot = :old",
                        ExpressionAttributeValues={":new": new, ":old": previous},
                    )
                return slot
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                    raise
