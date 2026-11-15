"""SQS-triggered sender: one ``sg-notify-send`` batch (size <= 10) -> provider.

Partial batch failure (Powertools ``BatchProcessor``; the event source mapping reports batch item
failures): a record whose processing raises -- a transient provider error, a DB error, or a
malformed body -- is returned to the queue and retried; after 5 receives SQS moves it to the DLQ
``sg-notify-send-dlq``. Permanent provider errors are recorded as FAILED and do not raise.
Reserved concurrency on the function caps the send rate to the provider.
"""

from __future__ import annotations

import json
from typing import Any

from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.batch import (
    BatchProcessor,
    EventType,
    process_partial_response,
)
from aws_lambda_powertools.utilities.data_classes.sqs_event import SQSRecord
from aws_lambda_powertools.utilities.typing import LambdaContext

from ..api import deps
from ..services import sender

logger = Logger(service="notification-service")
processor = BatchProcessor(event_type=EventType.SQS)


def record_handler(record: SQSRecord) -> str:
    body: dict[str, Any] = json.loads(record.body)
    return sender.handle(deps.get_engine(), deps.get_provider(), body)


@logger.inject_lambda_context
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    result = process_partial_response(
        event=event, record_handler=record_handler, processor=processor, context=context
    )
    return dict(result)
