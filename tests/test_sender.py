"""The SQS sender: idempotency, BILL visits, PROMO consent + frequency cap, template checks,
transient (retry) vs permanent errors, OTP redaction, CONSENT messages, partial batch failure."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest
from sg_commons.db import tenant_session
from sqlalchemy import text

from helpers import add_template, admin_exec, admin_fetch, consent_msg, new_tenant, phone, send_msg
from notification_service.providers.base import PermanentProviderError, TransientProviderError
from notification_service.providers.fake import FakeProvider
from notification_service.services import sender


def _row(admin_url: str, tenant_id: UUID, ref_id: str) -> Any:
    return admin_fetch(
        admin_url,
        "SELECT status, skip_reason, error, attempts, params, buttons, provider_msg_id, "
        "customer_id, category FROM message WHERE tenant_id = :t AND ref_id = :r",
        t=tenant_id,
        r=ref_id,
    )[0]


def test_bill_sent_once_and_counts_a_visit(
    admin_url: str, engine: Any, provider: FakeProvider
) -> None:
    t = new_tenant(admin_url)
    to = phone()
    msg = send_msg(t, to, "BILL", customer_name="Ravi", buttons=[{"type": "URL", "value": "tok"}])
    assert sender.handle(engine, provider, msg) == "SENT"
    assert sender.handle(engine, provider, msg) == "SENT"  # duplicate delivery: not re-sent
    assert len(provider.sent) == 1
    sent = provider.sent[0]
    assert (sent.to, sent.template_name, sent.params) == (to, "sg_bill", msg["params"])
    assert sent.buttons[0].value == "tok"
    row = _row(admin_url, t, msg["ref_id"])
    assert (row.status, row.provider_msg_id, row.attempts, row.category) == (
        "SENT",
        sent.provider_msg_id,
        1,
        "UTILITY",
    )
    cust = admin_fetch(
        admin_url, "SELECT name, visit_count FROM customer WHERE id = :c", c=row.customer_id
    )[0]
    assert (cust.name, cust.visit_count) == ("Ravi", 1)
    # a second bill (new ticket) is a second visit
    sender.handle(engine, provider, send_msg(t, to, "BILL"))
    assert (
        admin_fetch(admin_url, "SELECT visit_count FROM customer WHERE id = :c", c=row.customer_id)[
            0
        ][0]
        == 2
    )
    ref = admin_fetch(
        admin_url,
        "SELECT tenant_id FROM message_ref WHERE provider_msg_id = :p",
        p=sent.provider_msg_id,
    )
    assert ref[0][0] == t


def test_otp_params_and_buttons_redacted(
    admin_url: str, engine: Any, provider: FakeProvider
) -> None:
    t = new_tenant(admin_url)
    msg = send_msg(
        t,
        phone(),
        "OTP",
        params=["482913"],
        ref_type="otp_challenge",
        buttons=[{"type": "COPY_CODE", "value": "482913"}],
    )
    assert sender.handle(engine, provider, msg) == "SENT"
    assert provider.sent[0].params == ["482913"]  # the real code goes to the provider
    assert provider.sent[0].buttons[0].value == "482913"
    row = _row(admin_url, t, msg["ref_id"])
    assert row.params == ["******"]
    assert row.buttons == [{"type": "COPY_CODE", "value": "******"}]
    assert "482913" not in json.dumps([str(v) for v in row])


def test_otp_redacted_even_when_template_missing(
    admin_url: str, engine: Any, provider: FakeProvider
) -> None:
    t = new_tenant(admin_url)
    msg = send_msg(t, phone(), "OTP", params=["111111"], language="ta")
    assert sender.handle(engine, provider, msg) == "FAILED"
    row = _row(admin_url, t, msg["ref_id"])
    assert (row.skip_reason, row.params) == ("TEMPLATE_NOT_FOUND", ["******"])
    assert provider.sent == []


def test_template_not_approved_fails(admin_url: str, engine: Any, provider: FakeProvider) -> None:
    t = new_tenant(admin_url)
    add_template(admin_url, t, "PROMO_DIWALI", status="PENDING")
    to = phone()
    sender.handle(engine, provider, consent_msg(t, to))
    msg = send_msg(t, to, "PROMO", template_code="PROMO_DIWALI")
    assert sender.handle(engine, provider, msg) == "FAILED"
    assert _row(admin_url, t, msg["ref_id"]).skip_reason == "TEMPLATE_NOT_APPROVED"


def test_tenant_template_overrides_platform(
    admin_url: str, engine: Any, provider: FakeProvider
) -> None:
    t = new_tenant(admin_url)
    add_template(admin_url, t, "BILL", category="UTILITY", name="acme_bill")
    sender.handle(engine, provider, send_msg(t, phone(), "BILL"))
    assert provider.sent[0].template_name == "acme_bill"


def test_promo_requires_opt_in(admin_url: str, engine: Any, provider: FakeProvider) -> None:
    t = new_tenant(admin_url)
    add_template(admin_url, t, "PROMO_WEEKEND")
    to = phone()
    unknown = send_msg(t, to, "PROMO")
    assert sender.handle(engine, provider, unknown) == "SKIPPED"
    assert _row(admin_url, t, unknown["ref_id"]).skip_reason == "NO_CONSENT"

    assert sender.handle(engine, provider, consent_msg(t, to, "OPTED_OUT")) == "CONSENT"
    out = send_msg(t, to, "PROMO")
    assert sender.handle(engine, provider, out) == "SKIPPED"

    sender.handle(engine, provider, consent_msg(t, to, "OPTED_IN"))
    ok = send_msg(t, to, "PROMO")
    assert sender.handle(engine, provider, ok) == "SENT"
    assert len(provider.sent) == 1


def test_promo_frequency_cap(admin_url: str, engine: Any, provider: FakeProvider) -> None:
    t = new_tenant(admin_url)
    add_template(admin_url, t, "PROMO_WEEKEND")
    to = phone()
    sender.handle(engine, provider, consent_msg(t, to))
    assert sender.handle(engine, provider, send_msg(t, to, "PROMO")) == "SENT"
    capped = send_msg(t, to, "PROMO")
    assert sender.handle(engine, provider, capped) == "SKIPPED"
    assert _row(admin_url, t, capped["ref_id"]).skip_reason == "FREQUENCY_CAP"
    # bills don't count towards / aren't limited by the promo cap
    assert sender.handle(engine, provider, send_msg(t, to, "BILL")) == "SENT"
    # cap 0 = no cap
    admin_exec(
        admin_url,
        "INSERT INTO tenant_notify_settings (tenant_id, promo_cap_days) VALUES (:t, 0)",
        t=t,
    )
    assert sender.handle(engine, provider, send_msg(t, to, "PROMO")) == "SENT"
    # an older promo (outside the window) doesn't cap
    admin_exec(
        admin_url, "UPDATE tenant_notify_settings SET promo_cap_days = 7 WHERE tenant_id = :t", t=t
    )
    admin_exec(
        admin_url,
        "UPDATE message SET sent_at = now() - interval '8 days' "
        "WHERE tenant_id = :t AND purpose = 'PROMO'",
        t=t,
    )
    assert sender.handle(engine, provider, send_msg(t, to, "PROMO")) == "SENT"


def test_transient_raises_then_retry_sends_once(
    admin_url: str, engine: Any, provider: FakeProvider
) -> None:
    t = new_tenant(admin_url)
    msg = send_msg(t, phone(), "BILL")
    provider.fail_next.append(TransientProviderError("HTTP 503", code="503"))
    with pytest.raises(TransientProviderError):
        sender.handle(engine, provider, msg)
    row = _row(admin_url, t, msg["ref_id"])
    assert (row.status, row.attempts, row.error) == ("QUEUED", 1, "HTTP 503")
    assert sender.handle(engine, provider, msg) == "SENT"  # SQS redelivery
    row = _row(admin_url, t, msg["ref_id"])
    assert (row.status, row.attempts, row.error) == ("SENT", 2, None)
    assert len(provider.sent) == 1
    # the visit was counted once despite two deliveries
    assert (
        admin_fetch(admin_url, "SELECT visit_count FROM customer WHERE id = :c", c=row.customer_id)[
            0
        ][0]
        == 1
    )


def test_permanent_marks_failed(admin_url: str, engine: Any, provider: FakeProvider) -> None:
    t = new_tenant(admin_url)
    msg = send_msg(t, phone(), "BILL")
    provider.fail_next.append(PermanentProviderError("HTTP 400: not on WhatsApp", code="131026"))
    assert sender.handle(engine, provider, msg) == "FAILED"
    row = _row(admin_url, t, msg["ref_id"])
    assert (row.status, row.error) == ("FAILED", "HTTP 400: not on WhatsApp")
    assert sender.handle(engine, provider, msg) == "FAILED"  # redelivery: no new attempt
    assert _row(admin_url, t, msg["ref_id"]).attempts == 1


def test_idempotency_key_is_per_purpose_ref_phone(
    admin_url: str, engine: Any, provider: FakeProvider
) -> None:
    t = new_tenant(admin_url)
    to, ref = phone(), str(uuid4())
    sender.handle(engine, provider, send_msg(t, to, "BILL", ref_id=ref))
    sender.handle(engine, provider, send_msg(t, phone(), "BILL", ref_id=ref))  # other phone
    sender.handle(engine, provider, send_msg(t, to, "OTP", ref_id=ref))  # other purpose
    sender.handle(engine, provider, send_msg(t, to, "BILL", ref_id=ref))  # duplicate
    assert len(provider.sent) == 3


def test_consent_message_upserts_customer(
    admin_url: str, engine: Any, provider: FakeProvider
) -> None:
    t = new_tenant(admin_url)
    to = phone()
    outlet = uuid4()
    assert sender.handle(engine, provider, consent_msg(t, to, outlet_id=str(outlet))) == "CONSENT"
    sender.handle(engine, provider, consent_msg(t, to))  # redelivery: no extra row
    with tenant_session(engine, t) as s:
        rows = s.execute(text("SELECT status, source, outlet_id FROM customer_consent")).all()
        cust = s.execute(text("SELECT name, visit_count, first_seen_outlet_id FROM customer")).one()
    assert [(r.status, r.source, r.outlet_id) for r in rows] == [("OPTED_IN", "POS", outlet)]
    assert (cust.name, cust.visit_count, cust.first_seen_outlet_id) == ("Ravi", 0, outlet)


# ---- the Lambda handler (partial batch failure) ----


@dataclass
class _Ctx:
    function_name: str = "sender"
    memory_limit_in_mb: int = 512
    invoked_function_arn: str = "arn:aws:lambda:ap-south-1:0:function:sender"
    aws_request_id: str = "req"


def _record(body: dict[str, Any] | str, mid: str) -> dict[str, Any]:
    return {
        "messageId": mid,
        "receiptHandle": "rh",
        "body": body if isinstance(body, str) else json.dumps(body),
        "attributes": {"ApproximateReceiveCount": "1"},
        "messageAttributes": {},
        "md5OfBody": "",
        "eventSource": "aws:sqs",
        "eventSourceARN": "arn:aws:sqs:ap-south-1:0:sg-notify-send",
        "awsRegion": "ap-south-1",
    }


def test_handler_reports_only_failed_items(
    admin_url: str, engine: Any, provider: FakeProvider, wired: object
) -> None:
    from notification_service.handlers import sender_handler

    t = new_tenant(admin_url)
    ok = send_msg(t, phone(), "BILL")
    flaky = send_msg(t, phone(), "BILL")
    provider.fail_next.extend([])
    event = {"Records": [_record(ok, "m1"), _record("not json", "m2"), _record(flaky, "m3")]}

    original = provider.send_template
    calls = {"n": 0}

    def flaky_send(*args: Any, **kwargs: Any) -> str:
        calls["n"] += 1
        if calls["n"] == 2:
            raise TransientProviderError("throttled", code="130429")
        return original(*args, **kwargs)

    provider.send_template = flaky_send  # type: ignore[method-assign]
    out = sender_handler.handler(event, _Ctx())  # type: ignore[arg-type]
    assert out == {"batchItemFailures": [{"itemIdentifier": "m2"}, {"itemIdentifier": "m3"}]}
    assert len(provider.sent) == 1
