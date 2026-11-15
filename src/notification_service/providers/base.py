"""Provider-neutral WhatsApp interface.

Adapters (Meta Cloud API now; Gupshup / Karix / Route Mobile / Kaleyra once quotes are in)
implement :class:`WhatsAppProvider`. The sender and webhook code only see this module's types.

Error contract for sends:

* :class:`TransientProviderError` -- worth retrying (timeouts, 5xx, 429 / rate-limit codes, an
  expired access token). The sender re-raises so SQS redelivers; after 5 receives -> DLQ.
* :class:`PermanentProviderError` -- this message will never go (bad number, template mismatch,
  recipient not on WhatsApp). The sender marks it FAILED.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any


class ProviderError(Exception):
    """Base for provider failures. ``code`` is the provider's error code when it has one."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class TransientProviderError(ProviderError):
    pass


class PermanentProviderError(ProviderError):
    pass


class WebhookVerificationError(Exception):
    """Bad signature / verify token. The route answers 401/403 without detail."""


@dataclass(frozen=True)
class Button:
    """``type`` URL (dynamic URL suffix) or COPY_CODE (authentication OTP)."""

    type: str
    value: str


@dataclass(frozen=True)
class StatusUpdate:
    provider_msg_id: str
    status: str  # one of MessageStatus SENT / DELIVERED / READ / FAILED
    at: datetime
    error: str | None = None


@dataclass(frozen=True)
class InboundMessage:
    provider_msg_id: str
    from_e164: str
    text: str
    at: datetime


WebhookEvent = StatusUpdate | InboundMessage


class WhatsAppProvider(ABC):
    """One configured sender (the shared platform number)."""

    name: str

    @abstractmethod
    def send_template(
        self,
        to_e164: str,
        template_name: str,
        language: str,
        params: list[str] | dict[str, str],
        buttons: list[Button],
    ) -> str:
        """Send an approved template; returns the provider's message id."""

    @abstractmethod
    def send_text(self, to_e164: str, text: str) -> str:
        """Free-form text (only valid inside the 24-hour customer-service window)."""

    @abstractmethod
    def verify_challenge(self, query: Mapping[str, str]) -> str:
        """Subscription handshake (GET); returns the body to echo or raises
        :class:`WebhookVerificationError`."""

    @abstractmethod
    def verify_webhook(
        self, headers: Mapping[str, str], raw_body: bytes, query: Mapping[str, str]
    ) -> None:
        """Authenticate a webhook POST; raises :class:`WebhookVerificationError`."""

    @abstractmethod
    def parse_webhook(self, body: Any) -> list[WebhookEvent]:
        """Status updates and inbound messages in a verified webhook body."""
