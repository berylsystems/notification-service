"""In-memory provider for tests and local dev: records every send, never touches the network.

Webhook verification and parsing reuse the Meta format (same signature header and body shape), so
route tests can sign payloads with the configured ``app_secret``. ``fail_next`` scripts errors:
push a :class:`ProviderError` and the next send raises it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from .base import Button, ProviderError
from .meta_cloud import MetaCloudProvider


@dataclass
class SentRecord:
    to: str
    kind: str  # "template" | "text"
    template_name: str | None = None
    language: str | None = None
    params: list[str] | dict[str, str] = field(default_factory=list)
    buttons: list[Button] = field(default_factory=list)
    text: str | None = None
    provider_msg_id: str = ""


class FakeProvider(MetaCloudProvider):
    name = "fake"

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        super().__init__(config or {}, transport=self._no_network)
        self.sent: list[SentRecord] = []
        self.fail_next: list[ProviderError] = []

    @staticmethod
    def _no_network(url: str, body: bytes, headers: dict[str, str]) -> tuple[int, bytes]:
        raise AssertionError("FakeProvider never calls the network")

    def _next_id(self) -> str:
        if self.fail_next:
            raise self.fail_next.pop(0)
        return f"wamid.fake.{uuid4().hex}"

    def send_template(
        self,
        to_e164: str,
        template_name: str,
        language: str,
        params: list[str] | dict[str, str],
        buttons: list[Button],
    ) -> str:
        msg_id = self._next_id()
        self.sent.append(
            SentRecord(to_e164, "template", template_name, language, params, buttons,
                       provider_msg_id=msg_id)
        )  # fmt: skip
        return msg_id

    def send_text(self, to_e164: str, text: str) -> str:
        msg_id = self._next_id()
        self.sent.append(SentRecord(to_e164, "text", text=text, provider_msg_id=msg_id))
        return msg_id
