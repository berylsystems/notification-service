"""Meta WhatsApp Cloud API adapter.

* Send: ``POST https://graph.facebook.com/{graph_version}/{phone_number_id}/messages`` with a
  bearer access token. Templates carry body parameters (positional list, or named dict) and button
  parameters: a URL button's dynamic suffix, or an authentication template's copy-code value (Meta
  models both as ``sub_type: "url"`` button components, indexed by position).
* Webhook GET: ``hub.mode=subscribe`` + ``hub.verify_token`` (compared in constant time) ->
  echo ``hub.challenge``.
* Webhook POST: ``X-Hub-Signature-256: sha256=<hex>`` = HMAC-SHA256(app_secret, raw body),
  compared in constant time.

HTTP goes through stdlib ``urllib`` (keeps the Lambda bundle small); tests inject a transport.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from ..domain.enums import MessageStatus, normalize_phone
from .base import (
    Button,
    InboundMessage,
    PermanentProviderError,
    StatusUpdate,
    TransientProviderError,
    WebhookEvent,
    WebhookVerificationError,
    WhatsAppProvider,
)

# (url, body, headers) -> (http status, response body)
Transport = Callable[[str, bytes, dict[str, str]], tuple[int, bytes]]

GRAPH_BASE = "https://graph.facebook.com"
_STATUS_MAP = {
    "sent": MessageStatus.SENT.value,
    "delivered": MessageStatus.DELIVERED.value,
    "read": MessageStatus.READ.value,
    "failed": MessageStatus.FAILED.value,
}
# Graph / WhatsApp error codes worth retrying: generic/unknown (1, 2), throttling (4, 80007,
# 130429, 131048 spam-rate, 131056 pair-rate), expired/invalid token (190: a config problem --
# retrying sends the batch to the DLQ for redrive instead of losing it), service unavailable
# (131000, 133004).
_TRANSIENT_CODES = {1, 2, 4, 190, 80007, 130429, 131000, 131048, 131056, 133004}


def urllib_transport(timeout: float) -> Transport:
    def send(url: str, body: bytes, headers: dict[str, str]) -> tuple[int, bytes]:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed host)
                return int(resp.status), bytes(resp.read())
        except urllib.error.HTTPError as exc:
            return int(exc.code), bytes(exc.read() or b"")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TransientProviderError(f"network error: {type(exc).__name__}") from exc

    return send


def _header(headers: Mapping[str, str], name: str) -> str | None:
    lname = name.lower()
    for key, value in headers.items():
        if key.lower() == lname:
            return value
    return None


def _ts(raw: object) -> datetime:
    try:
        return datetime.fromtimestamp(int(str(raw)), tz=UTC)
    except (TypeError, ValueError):
        return datetime.now(UTC)


class MetaCloudProvider(WhatsAppProvider):
    name = "meta"

    def __init__(self, config: Mapping[str, Any], transport: Transport) -> None:
        self._token = str(config.get("access_token") or "")
        self._phone_number_id = str(config.get("phone_number_id") or "")
        self._app_secret = str(config.get("app_secret") or "").encode()
        self._verify_token = str(config.get("verify_token") or "")
        self._graph_version = str(config.get("graph_version") or "v21.0")
        self._transport = transport

    # ---- payloads (pure; tested against expected JSON) ----

    @property
    def messages_url(self) -> str:
        return f"{GRAPH_BASE}/{self._graph_version}/{self._phone_number_id}/messages"

    @staticmethod
    def template_payload(
        to_e164: str,
        template_name: str,
        language: str,
        params: list[str] | dict[str, str],
        buttons: list[Button],
    ) -> dict[str, Any]:
        components: list[dict[str, Any]] = []
        if params:
            if isinstance(params, dict):
                body_params = [
                    {"type": "text", "parameter_name": k, "text": str(v)} for k, v in params.items()
                ]
            else:
                body_params = [{"type": "text", "text": str(v)} for v in params]
            components.append({"type": "body", "parameters": body_params})
        for index, button in enumerate(buttons):
            if button.type not in ("URL", "COPY_CODE"):
                raise PermanentProviderError(f"unsupported button type {button.type}")
            components.append(
                {
                    "type": "button",
                    "sub_type": "url",
                    "index": str(index),
                    "parameters": [{"type": "text", "text": button.value}],
                }
            )
        template: dict[str, Any] = {"name": template_name, "language": {"code": language}}
        if components:
            template["components"] = components
        return {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_e164.lstrip("+"),
            "type": "template",
            "template": template,
        }

    @staticmethod
    def text_payload(to_e164: str, text: str) -> dict[str, Any]:
        return {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_e164.lstrip("+"),
            "type": "text",
            "text": {"preview_url": False, "body": text},
        }

    # ---- sending ----

    def _post(self, payload: dict[str, Any]) -> str:
        status, raw = self._transport(
            self.messages_url,
            json.dumps(payload, separators=(",", ":")).encode(),
            {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"},
        )
        try:
            data: dict[str, Any] = json.loads(raw or b"{}")
        except ValueError:
            data = {}
        if 200 <= status < 300:
            try:
                return str(data["messages"][0]["id"])
            except (KeyError, IndexError, TypeError) as exc:
                raise TransientProviderError("response without a message id") from exc
        err = data.get("error") or {}
        code = err.get("code")
        detail = f"HTTP {status}: {err.get('message') or 'error'}"[:300]
        if status == 429 or status >= 500 or status == 401 or code in _TRANSIENT_CODES:
            raise TransientProviderError(detail, code=str(code) if code else None)
        raise PermanentProviderError(detail, code=str(code) if code else None)

    def send_template(
        self,
        to_e164: str,
        template_name: str,
        language: str,
        params: list[str] | dict[str, str],
        buttons: list[Button],
    ) -> str:
        return self._post(self.template_payload(to_e164, template_name, language, params, buttons))

    def send_text(self, to_e164: str, text: str) -> str:
        return self._post(self.text_payload(to_e164, text))

    # ---- webhooks ----

    def verify_challenge(self, query: Mapping[str, str]) -> str:
        token = query.get("hub.verify_token") or ""
        if query.get("hub.mode") != "subscribe" or not self._verify_token:
            raise WebhookVerificationError("bad mode")
        if not hmac.compare_digest(token.encode(), self._verify_token.encode()):
            raise WebhookVerificationError("bad verify token")
        return query.get("hub.challenge") or ""

    def signature(self, raw_body: bytes) -> str:
        return "sha256=" + hmac.new(self._app_secret, raw_body, hashlib.sha256).hexdigest()

    def verify_webhook(
        self, headers: Mapping[str, str], raw_body: bytes, query: Mapping[str, str]
    ) -> None:
        given = _header(headers, "x-hub-signature-256") or ""
        if not self._app_secret or not given:
            raise WebhookVerificationError("missing signature")
        if not hmac.compare_digest(given.encode(), self.signature(raw_body).encode()):
            raise WebhookVerificationError("bad signature")

    def parse_webhook(self, body: Any) -> list[WebhookEvent]:
        out: list[WebhookEvent] = []
        if not isinstance(body, dict):
            return out
        for entry in body.get("entry") or []:
            for change in (entry or {}).get("changes") or []:
                value = (change or {}).get("value") or {}
                for st in value.get("statuses") or []:
                    status = _STATUS_MAP.get(str(st.get("status")))
                    if not status or not st.get("id"):
                        continue
                    errors = st.get("errors") or []
                    error = None
                    if errors:
                        e0 = errors[0] or {}
                        error = f"{e0.get('code')}: {e0.get('title') or e0.get('message')}"[:300]
                    out.append(StatusUpdate(str(st["id"]), status, _ts(st.get("timestamp")), error))
                for msg in value.get("messages") or []:
                    text = self._inbound_text(msg)
                    if not msg.get("id") or not msg.get("from"):
                        continue
                    out.append(
                        InboundMessage(
                            str(msg["id"]),
                            normalize_phone(str(msg["from"])),
                            text,
                            _ts(msg.get("timestamp")),
                        )
                    )
        return out

    @staticmethod
    def _inbound_text(msg: dict[str, Any]) -> str:
        kind = msg.get("type")
        if kind == "text":
            return str((msg.get("text") or {}).get("body") or "")
        if kind == "button":  # template quick reply ("Stop promotions")
            return str((msg.get("button") or {}).get("text") or "")
        if kind == "interactive":
            inter = msg.get("interactive") or {}
            reply = inter.get("button_reply") or inter.get("list_reply") or {}
            return str(reply.get("title") or "")
        return ""
