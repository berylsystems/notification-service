"""Publishing SEND messages onto SQS ``sg-notify-send`` (campaign fan-out)."""

from __future__ import annotations

import json
from typing import Any, Protocol


class Publisher(Protocol):
    def publish(self, messages: list[dict[str, Any]]) -> None:
        """Enqueue every message or raise."""


class SqsPublisher:
    """``SendMessageBatch`` in chunks of 10; any failed entry raises (the caller retries)."""

    def __init__(self, queue_url: str, region: str) -> None:
        import boto3  # type: ignore[import-untyped]

        self._client = boto3.client("sqs", region_name=region)
        self._url = queue_url

    def publish(self, messages: list[dict[str, Any]]) -> None:
        for start in range(0, len(messages), 10):
            chunk = messages[start : start + 10]
            resp = self._client.send_message_batch(
                QueueUrl=self._url,
                Entries=[
                    {"Id": str(i), "MessageBody": json.dumps(m, separators=(",", ":"))}
                    for i, m in enumerate(chunk)
                ],
            )
            if resp.get("Failed"):
                raise RuntimeError(f"SQS rejected {len(resp['Failed'])} of {len(chunk)} messages")


class ListPublisher:
    """Test / local double."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def publish(self, messages: list[dict[str, Any]]) -> None:
        self.messages.extend(messages)
