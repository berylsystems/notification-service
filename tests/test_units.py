"""Pure units (no DB): phone masking, OTP redaction, forward-only statuses, the estimate math,
the SQS contract, and the Meta adapter (payloads, error classification, webhook verify/parse)."""

from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from helpers import APP_SECRET, VERIFY_TOKEN, meta_inbound, meta_status, provider_config
from notification_service.domain import enums as e
from notification_service.domain.contract import ConsentMessage, SendMessage, parse_message
from notification_service.providers.base import (
    Button,
    InboundMessage,
    PermanentProviderError,
    StatusUpdate,
    TransientProviderError,
    WebhookVerificationError,
)
from notification_service.providers.fake import FakeProvider
from notification_service.providers.meta_cloud import MetaCloudProvider
from notification_service.providers.registry import build_provider
from notification_service.services.webhooks import is_stop


def test_mask_phone() -> None:
    assert e.mask_phone("+919876543210") == "+91******3210"
    assert e.mask_phone("+12345") == "****45"
    assert e.mask_phone(None) == ""


def test_redaction() -> None:
    assert e.redact_params(["123456"]) == ["******"]
    assert e.redact_params({"code": "1"}) == {"code": "******"}
    assert e.redact_buttons([{"type": "COPY_CODE", "value": "123456"}]) == [
        {"type": "COPY_CODE", "value": "******"}
    ]


@pytest.mark.parametrize(
    ("cur", "new", "ok"),
    [
        ("QUEUED", "SENT", True),
        ("SENT", "DELIVERED", True),
        ("DELIVERED", "READ", True),
        ("SENT", "READ", True),
        ("READ", "DELIVERED", False),
        ("DELIVERED", "SENT", False),
        ("SENT", "FAILED", True),
        ("DELIVERED", "FAILED", False),
        ("READ", "FAILED", False),
        ("FAILED", "DELIVERED", True),  # Meta can report failed then a late delivered
        ("SKIPPED", "SENT", False),
        ("READ", "READ", False),
    ],
)
def test_status_forward_only(cur: str, new: str, ok: bool) -> None:
    assert e.status_advances(cur, new) is ok


def test_estimate_math() -> None:
    out = e.estimate_cost(1000, "MARKETING", Decimal("0"))
    assert out["subtotal_paise"] == Decimal("86310.00")
    assert out["gst_paise"] == Decimal("15535.80")
    assert out["total_paise"] == Decimal("101845.80")  # ~ Rs 1,018.46
    out = e.estimate_cost(3, "UTILITY", Decimal("9"))
    assert out["subtotal_paise"] == Decimal("61.50")  # 3 x (11.5 + 9)
    assert out["gst_paise"] == Decimal("11.07")
    assert e.estimate_cost(0, "AUTHENTICATION", Decimal("0"))["total_paise"] == Decimal("0.00")


@pytest.mark.parametrize(
    ("text", "stop"),
    [
        ("STOP", True),
        (" stop ", True),
        ("Stop All", True),
        ("UNSUBSCRIBE", True),
        ("stop  promotions", True),
        ("Stop promotions", True),
        ("START", False),
        ("please stop", False),
        ("", False),
    ],
)
def test_is_stop(text: str, stop: bool) -> None:
    assert is_stop(text) is stop


# ---- contract ----


def test_contract_accepts_the_otp_producer_message() -> None:
    body = {
        "v": 1,
        "type": "SEND",
        "purpose": "OTP",
        "tenant_id": str(uuid4()),
        "outlet_id": None,
        "to": "+919876543210",
        "template_code": "OTP",
        "language": "en",
        "params": ["123456"],
        "buttons": [{"type": "COPY_CODE", "value": "123456"}],
        "ref_type": "otp_challenge",
        "ref_id": str(uuid4()),
    }
    msg = parse_message(body)
    assert isinstance(msg, SendMessage)
    assert msg.purpose is e.Purpose.OTP and msg.buttons[0].type == "COPY_CODE"


def test_contract_type_defaults_to_send_and_consent_parses() -> None:
    tid = str(uuid4())
    send = parse_message(
        {
            "v": 1,
            "purpose": "BILL",
            "tenant_id": tid,
            "to": "+919800000001",
            "template_code": "BILL",
            "ref_id": "t1",
            "extra": "ignored",
        }
    )
    assert isinstance(send, SendMessage)
    consent = parse_message(
        {
            "v": 1,
            "type": "CONSENT",
            "tenant_id": tid,
            "phone": "+919800000001",
            "status": "OPTED_IN",
            "source": "POS",
        }
    )
    assert isinstance(consent, ConsentMessage)


@pytest.mark.parametrize(
    "patch",
    [
        {"v": 2},
        {"to": "919800000001"},
        {"purpose": "SPAM"},
        {"template_code": "promo"},
        {"ref_id": ""},
        {"buttons": [{"type": "CALL", "value": "x"}]},
    ],
)
def test_contract_rejects(patch: dict[str, object]) -> None:
    body = {
        "v": 1,
        "purpose": "BILL",
        "tenant_id": str(uuid4()),
        "to": "+919800000001",
        "template_code": "BILL",
        "ref_id": "t1",
        **patch,
    }
    with pytest.raises(ValidationError):
        parse_message(body)


# ---- Meta adapter ----


class Recorder:
    def __init__(self, status: int = 200, body: object | None = None) -> None:
        self.status = status
        self.body = body if body is not None else {"messages": [{"id": "wamid.ABC"}]}
        self.calls: list[tuple[str, dict[str, object], dict[str, str]]] = []

    def __call__(self, url: str, body: bytes, headers: dict[str, str]) -> tuple[int, bytes]:
        self.calls.append((url, json.loads(body), headers))
        return self.status, json.dumps(self.body).encode()


def _meta(rec: Recorder) -> MetaCloudProvider:
    return MetaCloudProvider(provider_config("meta"), rec)


def test_meta_bill_payload() -> None:
    rec = Recorder()
    msg_id = _meta(rec).send_template(
        "+919876543210",
        "sg_bill",
        "en",
        ["Andheri Foods", "A-12", "540.00"],
        [Button("URL", "tok123")],
    )
    assert msg_id == "wamid.ABC"
    url, payload, headers = rec.calls[0]
    assert url == "https://graph.facebook.com/v21.0/1234567890/messages"
    assert headers["Authorization"] == "Bearer test-token"
    assert payload == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "919876543210",
        "type": "template",
        "template": {
            "name": "sg_bill",
            "language": {"code": "en"},
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": "Andheri Foods"},
                        {"type": "text", "text": "A-12"},
                        {"type": "text", "text": "540.00"},
                    ],
                },
                {
                    "type": "button",
                    "sub_type": "url",
                    "index": "0",
                    "parameters": [{"type": "text", "text": "tok123"}],
                },
            ],
        },
    }


def test_meta_otp_copy_code_and_named_params() -> None:
    otp = MetaCloudProvider.template_payload(
        "+919876543210", "sg_otp", "en", ["123456"], [Button("COPY_CODE", "123456")]
    )
    assert otp["template"]["components"] == [
        {"type": "body", "parameters": [{"type": "text", "text": "123456"}]},
        {
            "type": "button",
            "sub_type": "url",
            "index": "0",
            "parameters": [{"type": "text", "text": "123456"}],
        },
    ]
    named = MetaCloudProvider.template_payload("+91980", "t", "hi", {"store": "X"}, [])
    assert named["template"] == {
        "name": "t",
        "language": {"code": "hi"},
        "components": [
            {
                "type": "body",
                "parameters": [{"type": "text", "parameter_name": "store", "text": "X"}],
            }
        ],
    }
    bare = MetaCloudProvider.template_payload("+91980", "t", "en", [], [])
    assert "components" not in bare["template"]


def test_meta_text_payload() -> None:
    rec = Recorder()
    _meta(rec).send_text("+919876543210", "bye")
    assert rec.calls[0][1] == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "919876543210",
        "type": "text",
        "text": {"preview_url": False, "body": "bye"},
    }


@pytest.mark.parametrize(
    ("status", "code", "kind"),
    [
        (500, None, TransientProviderError),
        (429, None, TransientProviderError),
        (400, 130429, TransientProviderError),
        (401, 190, TransientProviderError),
        (400, 131026, PermanentProviderError),
        (400, 132001, PermanentProviderError),
    ],
)
def test_meta_error_classification(status: int, code: int | None, kind: type[Exception]) -> None:
    rec = Recorder(status, {"error": {"code": code, "message": "nope"}})
    with pytest.raises(kind):
        _meta(rec).send_text("+919876543210", "x")


def test_meta_webhook_verify_challenge() -> None:
    p = _meta(Recorder())
    q = {"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "42"}
    assert p.verify_challenge(q) == "42"
    with pytest.raises(WebhookVerificationError):
        p.verify_challenge({**q, "hub.verify_token": "wrong"})
    with pytest.raises(WebhookVerificationError):
        p.verify_challenge({**q, "hub.mode": "unsubscribe"})


def test_meta_webhook_signature() -> None:
    p = _meta(Recorder())
    raw = b'{"entry":[]}'
    good = "sha256=" + hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    p.verify_webhook({"X-Hub-Signature-256": good}, raw, {})
    for headers in ({}, {"x-hub-signature-256": "sha256=00"}, {"x-hub-signature-256": good[:-1]}):
        with pytest.raises(WebhookVerificationError):
            p.verify_webhook(headers, raw, {})
    with pytest.raises(WebhookVerificationError):
        p.verify_webhook({"x-hub-signature-256": good}, raw + b" ", {})


def test_meta_parse_webhook() -> None:
    p = _meta(Recorder())
    events = p.parse_webhook(meta_status("wamid.1", "read"))
    assert isinstance(events[0], StatusUpdate)
    assert (events[0].provider_msg_id, events[0].status) == ("wamid.1", "READ")
    inbound = p.parse_webhook(meta_inbound("+919800000001", "STOP", "wamid.in"))[0]
    assert isinstance(inbound, InboundMessage)
    assert (inbound.from_e164, inbound.text) == ("+919800000001", "STOP")
    button = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": "919800000001",
                                    "id": "w2",
                                    "type": "button",
                                    "button": {"text": "Stop promotions"},
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }
    assert p.parse_webhook(button)[0].text == "Stop promotions"  # type: ignore[union-attr]
    failed = meta_status("wamid.2", "failed")
    failed["entry"][0]["changes"][0]["value"]["statuses"][0]["errors"] = [
        {"code": 131026, "title": "Message undeliverable"}
    ]
    assert p.parse_webhook(failed)[0].error == "131026: Message undeliverable"  # type: ignore[union-attr]
    assert p.parse_webhook(meta_status("wamid.3", "deleted")) == []
    assert p.parse_webhook("nonsense") == []


def test_registry() -> None:
    assert isinstance(build_provider(provider_config("meta")), MetaCloudProvider)
    assert isinstance(build_provider(provider_config("fake")), FakeProvider)
    with pytest.raises(RuntimeError):
        build_provider({"provider": "gupshup"})
