"""Lambda entrypoint for the notification-service HTTP API (admin routes + provider webhooks)."""

from __future__ import annotations

from typing import Any

from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext

from ..api.app import app

logger = Logger(service="notification-service")


@logger.inject_lambda_context
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    return app.resolve(event, context)
