"""The job queue on SQS.

Retries, visibility timeouts and dead-lettering are SQS's own: the redrive
policy on the queue moves a message to the dead-letter queue after
``maxReceiveCount`` deliveries, whether the worker failed or crashed.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import boto3

from ..jobs.queue import Message, QueueDepth

UTC = timezone.utc
MAX_BATCH = 10  # SQS's ceiling for one receive
MAX_WAIT_S = 20  # and for one long poll


class SqsJobQueue:
    def __init__(
        self,
        queue_url: str,
        region: str,
        endpoint_url: str | None = None,
        dlq_url: str | None = None,
    ) -> None:
        self.queue_url = queue_url
        self.dlq_url = dlq_url or None
        self._sqs = boto3.client("sqs", region_name=region, endpoint_url=endpoint_url)

    def close(self) -> None:
        return None

    def send(self, body: dict[str, Any], delay_s: int = 0) -> None:
        self._sqs.send_message(
            QueueUrl=self.queue_url, MessageBody=json.dumps(body), DelaySeconds=int(delay_s)
        )

    def receive(self, max_messages: int = 1, wait_s: float = 0.0) -> list[Message]:
        response = self._sqs.receive_message(
            QueueUrl=self.queue_url,
            MaxNumberOfMessages=max(1, min(max_messages, MAX_BATCH)),
            WaitTimeSeconds=int(min(max(wait_s, 0), MAX_WAIT_S)),
            AttributeNames=["ApproximateReceiveCount", "SentTimestamp"],
        )
        return [self._message(m) for m in response.get("Messages", [])]

    def ack(self, message: Message) -> None:
        self._sqs.delete_message(QueueUrl=self.queue_url, ReceiptHandle=message.receipt)

    def retry_later(self, message: Message, delay_s: int) -> None:
        self._sqs.change_message_visibility(
            QueueUrl=self.queue_url,
            ReceiptHandle=message.receipt,
            VisibilityTimeout=int(max(0, min(delay_s, 43_200))),
        )

    def depth(self) -> QueueDepth:
        attrs = self._sqs.get_queue_attributes(
            QueueUrl=self.queue_url,
            AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
        )["Attributes"]
        dead = 0
        if self.dlq_url:
            dead = int(
                self._sqs.get_queue_attributes(
                    QueueUrl=self.dlq_url, AttributeNames=["ApproximateNumberOfMessages"]
                )["Attributes"]["ApproximateNumberOfMessages"]
            )
        return QueueDepth(
            visible=int(attrs["ApproximateNumberOfMessages"]),
            in_flight=int(attrs["ApproximateNumberOfMessagesNotVisible"]),
            dead=dead,
        )

    def dead_letters(self, limit: int = 20) -> list[Message]:
        """Peek at dead-lettered jobs without taking them off the DLQ."""
        if not self.dlq_url:
            return []
        response = self._sqs.receive_message(
            QueueUrl=self.dlq_url,
            MaxNumberOfMessages=max(1, min(limit, MAX_BATCH)),
            VisibilityTimeout=0,
            AttributeNames=["ApproximateReceiveCount", "SentTimestamp"],
        )
        return [self._message(m) for m in response.get("Messages", [])]

    @staticmethod
    def _message(raw: dict[str, Any]) -> Message:
        attrs = raw.get("Attributes", {})
        try:
            body = json.loads(raw["Body"])
        except ValueError:
            body = {"raw": raw["Body"]}
        return Message(
            id=raw["MessageId"],
            body=body if isinstance(body, dict) else {"raw": body},
            receive_count=int(attrs.get("ApproximateReceiveCount", 1)),
            receipt=raw["ReceiptHandle"],
            sent_at=datetime.fromtimestamp(int(attrs.get("SentTimestamp", 0)) / 1000, UTC),
        )
