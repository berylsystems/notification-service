"""EventBridge (rate 1 minute) scheduler: fan out due campaigns onto ``sg-notify-send``."""

from __future__ import annotations

from typing import Any

from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext

from ..api import deps
from ..config import get_notify_settings
from ..services import campaigns

logger = Logger(service="notification-service")


@logger.inject_lambda_context
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    out = campaigns.run_due(
        deps.get_engine(), deps.get_publisher(), page_size=get_notify_settings().campaign_page_size
    )
    return {"status": "ok", "campaigns": out}
