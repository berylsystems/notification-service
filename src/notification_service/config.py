"""Service-specific settings (prefix ``SG_NOTIFY_``) and the WhatsApp provider secret loader.

Shared settings (DB, JWT issuer/audience, KMS key, region) come from ``sg_commons.config``.

The provider secret (Secrets Manager ``sg/notification-service/whatsapp``) is a JSON object::

    {"provider": "meta", "access_token": "...", "phone_number_id": "...",
     "app_secret": "...", "verify_token": "...", "graph_version": "v21.0"}

It is loaded like ``sg_commons.device_tokens.load_device_secret``: the env var
``SG_NOTIFY_WHATSAPP_SECRET_NAME`` holds the secret's NAME (not a partial ARN: IAM's
``name-??????`` grant only matches once Secrets Manager resolves a name to the full ARN), the
value is cached per process, a fake-provider config is used locally, and a deployed Lambda
(shared signing key configured) without it fails closed.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from functools import lru_cache
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict
from sg_commons.config import Settings, get_settings

SECRET_NAME_ENV = "SG_NOTIFY_WHATSAPP_SECRET_NAME"
DEV_PROVIDER_CONFIG: dict[str, Any] = {
    "provider": "fake",
    "access_token": "dev",
    "phone_number_id": "dev",
    "app_secret": "dev-only-app-secret",
    "verify_token": "dev-only-verify-token",
    "graph_version": "v21.0",
}


class NotifySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SG_NOTIFY_", env_file=".env", extra="ignore")

    whatsapp_secret_name: str | None = None
    queue_url: str | None = None  # scheduler -> SQS notify-send
    # Provider fee per message on top of Meta's rate (Gupshup quotes ~9 paise); used by estimates.
    provider_markup_paise: Decimal = Decimal("0")
    campaign_page_size: int = 500
    http_timeout_seconds: float = 10.0


@lru_cache
def get_notify_settings() -> NotifySettings:
    return NotifySettings()


_cached: dict[str, Any] | None = None


def load_provider_config(settings: Settings | None = None) -> dict[str, Any]:
    """The provider secret as a dict (see module docstring). Never log its values."""
    global _cached
    if _cached is None:
        settings = settings or get_settings()
        name = os.environ.get(SECRET_NAME_ENV)
        if name:
            import boto3  # type: ignore[import-untyped]

            client = boto3.client("secretsmanager", region_name=settings.aws_region)
            raw = client.get_secret_value(SecretId=name)["SecretString"]
            data: dict[str, Any] = json.loads(raw)
            _cached = data
        elif settings.kms_signing_key_id:
            raise RuntimeError(f"{SECRET_NAME_ENV} is not configured")
        else:
            _cached = dict(DEV_PROVIDER_CONFIG)
    return _cached


def reset_provider_config() -> None:
    global _cached
    _cached = None
