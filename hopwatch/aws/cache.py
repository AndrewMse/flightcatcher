"""The Wizz response cache in DynamoDB, shared by every Lambda worker.

Values are zlib-compressed JSON: the full route map is several hundred KB of
JSON, over DynamoDB's 400 KB item limit, but compresses to a fraction of that.
Items expire through the table's TTL a week after writing, long after their
logical TTL, so ``get_stale`` still has something to fall back on when Wizz
is down.
"""

from __future__ import annotations

import json
import logging
import time
import zlib
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.types import Binary
from botocore.exceptions import ClientError

from .schema import table_name

log = logging.getLogger(__name__)
KEEP_S = 7 * 24 * 3600


class DynamoCache:
    def __init__(self, prefix: str, region: str, endpoint_url: str | None = None) -> None:
        self._table = boto3.resource(
            "dynamodb", region_name=region, endpoint_url=endpoint_url
        ).Table(table_name(prefix, "meta"))

    def _read(self, key: str) -> tuple[Any, float] | None:
        item = self._table.get_item(Key={"pk": f"cache#{key}"}).get("Item")
        if not item:
            return None
        raw = item["value"]
        data = raw.value if isinstance(raw, Binary) else bytes(raw)
        return json.loads(zlib.decompress(data)), float(item["stored_at"])

    def get(self, key: str, ttl: float) -> Any | None:
        found = self._read(key)
        if found is None:
            return None
        value, stored_at = found
        return value if time.time() - stored_at <= ttl else None

    def get_stale(self, key: str) -> Any | None:
        found = self._read(key)
        return found[0] if found else None

    def set(self, key: str, value: Any) -> None:
        now = time.time()
        try:
            self._table.put_item(
                Item={
                    "pk": f"cache#{key}",
                    "value": Binary(zlib.compress(json.dumps(value).encode(), 6)),
                    "stored_at": Decimal(repr(now)),
                    "expires_at": int(now + KEEP_S),
                }
            )
        except ClientError as exc:  # a broken cache must never break a search
            log.warning("cache write failed for %s: %s", key, exc)
