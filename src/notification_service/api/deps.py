"""Lazily-built, test-overridable runtime dependencies.

* engine / signer: as in the other services (the signer is only used for the shared key's public
  JWK, to verify admin tokens minted by restaurant-mgmt).
* provider: the WhatsApp adapter named by the provider secret (``config.load_provider_config``:
  Secrets Manager ``SG_NOTIFY_WHATSAPP_SECRET_NAME`` when deployed, fails closed without it; the
  fake provider locally).
* publisher: SQS ``SG_NOTIFY_QUEUE_URL`` (scheduler fan-out); a list double when unset locally.
"""

from __future__ import annotations

from sg_commons.config import get_settings
from sg_commons.crypto.signer import KmsSigner, LocalRsaSigner, Signer
from sg_commons.db import default_engine
from sqlalchemy import Engine

from ..config import get_notify_settings, load_provider_config
from ..providers.base import WhatsAppProvider
from ..providers.registry import build_provider
from ..services.queue import ListPublisher, Publisher, SqsPublisher

_engine: Engine | None = None
_signer: Signer | None = None
_provider: WhatsAppProvider | None = None
_publisher: Publisher | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = default_engine()
    return _engine


def get_signer() -> Signer:
    global _signer
    if _signer is None:
        settings = get_settings()
        if settings.kms_signing_key_id:
            _signer = KmsSigner(settings.kms_signing_key_id, region=settings.aws_region)
        else:
            _signer = LocalRsaSigner.ephemeral()  # local/dev only
    return _signer


def get_provider() -> WhatsAppProvider:
    global _provider
    if _provider is None:
        _provider = build_provider(
            load_provider_config(get_settings()),
            timeout=get_notify_settings().http_timeout_seconds,
        )
    return _provider


def get_publisher() -> Publisher:
    global _publisher
    if _publisher is None:
        url = get_notify_settings().queue_url
        if url:
            _publisher = SqsPublisher(url, get_settings().aws_region)
        elif get_settings().kms_signing_key_id:
            raise RuntimeError("SG_NOTIFY_QUEUE_URL is not configured")
        else:
            _publisher = ListPublisher()  # local/dev only
    return _publisher


def configure(
    *,
    engine: Engine | None = None,
    signer: Signer | None = None,
    provider: WhatsAppProvider | None = None,
    publisher: Publisher | None = None,
) -> None:
    global _engine, _signer, _provider, _publisher
    if engine is not None:
        _engine = engine
    if signer is not None:
        _signer = signer
    if provider is not None:
        _provider = provider
    if publisher is not None:
        _publisher = publisher


def reset() -> None:
    global _engine, _signer, _provider, _publisher
    _engine = _signer = _provider = _publisher = None
