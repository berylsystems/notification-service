"""Pick the adapter named by the provider secret's ``provider`` value."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .base import WhatsAppProvider
from .fake import FakeProvider
from .meta_cloud import MetaCloudProvider, Transport, urllib_transport

PROVIDERS = ("meta", "fake")


def build_provider(
    config: Mapping[str, Any], *, transport: Transport | None = None, timeout: float = 10.0
) -> WhatsAppProvider:
    name = str(config.get("provider") or "").lower()
    if name == "meta":
        return MetaCloudProvider(config, transport or urllib_transport(timeout))
    if name == "fake":
        return FakeProvider(config)
    raise RuntimeError(f"unknown WhatsApp provider {name!r} (supported: {', '.join(PROVIDERS)})")
