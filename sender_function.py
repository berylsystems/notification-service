"""AWS Lambda entrypoint shim for the SQS-triggered WhatsApp sender."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from notification_service.handlers.sender_handler import handler  # noqa: E402

__all__ = ["handler"]
