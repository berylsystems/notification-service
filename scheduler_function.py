"""AWS Lambda entrypoint shim for the once-a-minute campaign scheduler."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from notification_service.handlers.scheduler_handler import handler  # noqa: E402

__all__ = ["handler"]
