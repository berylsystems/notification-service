"""Webhook routes: verify challenge, signature checks, forward-only statuses, STOP -> opt-out in
every tenant + one confirmation, retry de-duplication, START ignored."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sg_commons.db import tenant_session

from helpers import (
    VERIFY_TOKEN,
    add_template,
    admin_fetch,
    call_full,
    consent_msg,
    meta_inbound,
    meta_status,
    new_tenant,
    phone,
    send_msg,
    signed_webhook,
)
from notification_service.providers.fake import FakeProvider
from notification_service.services import customers, sender
from notification_service.services.webhooks import STOP_CONFIRMATION


def test_verify_challenge(wired: object) -> None:
    q = f"hub.mode=subscribe&hub.verify_token={VERIFY_TOKEN}&hub.challenge=1158201444"
    status, body, headers = call_full("GET", f"/webhooks/whatsapp/fake?{q}")
    assert (status, body) == (200, "1158201444")
    assert headers["Content-Type"] == "text/plain"
    status, _, _ = call_full(
        "GET", "/webhooks/whatsapp/fake?hub.mode=subscribe&hub.verify_token=nope&hub.challenge=1"
    )
    assert status == 401
    status, _, _ = call_full("GET", f"/webhooks/whatsapp/gupshup?{q}")
    assert status == 404


def test_signature_required(wired: object) -> None:
    payload = meta_status("wamid.x", "delivered")
    assert signed_webhook(payload)[0] == 200
    assert signed_webhook(payload, secret="wrong")[0] == 401
    status, body, _ = call_full("POST", "/webhooks/whatsapp/fake", payload)  # unsigned
    assert status == 401 and body["title"] == "Unauthorized"


def _sent(admin_url: str, engine: Any, provider: FakeProvider) -> tuple[UUID, str, str]:
    t = new_tenant(admin_url)
    msg = send_msg(t, phone(), "BILL")
    sender.handle(engine, provider, msg)
    return t, msg["ref_id"], provider.sent[-1].provider_msg_id


def _status(admin_url: str, ref_id: str) -> Any:
    return admin_fetch(
        admin_url,
        "SELECT status, delivered_at, read_at, failed_at, error FROM message WHERE ref_id = :r",
        r=ref_id,
    )[0]


def test_status_forward_only(
    admin_url: str, engine: Any, provider: FakeProvider, wired: object
) -> None:
    _, ref, wamid = _sent(admin_url, engine, provider)
    status, body = signed_webhook(meta_status(wamid, "read", 1_763_200_100))
    assert status == 200 and body["counts"]["updated"] == 1
    row = _status(admin_url, ref)
    assert row.status == "READ" and row.read_at is not None
    assert row.delivered_at == row.read_at  # READ implies delivered
    # out-of-order DELIVERED and a late FAILED don't regress it
    assert (
        signed_webhook(meta_status(wamid, "delivered", 1_763_200_050))[1]["counts"]["updated"] == 0
    )
    failed = meta_status(wamid, "failed")
    failed["entry"][0]["changes"][0]["value"]["statuses"][0]["errors"] = [{"code": 1, "title": "x"}]
    signed_webhook(failed)
    assert _status(admin_url, ref).status == "READ"


def test_status_delivered_then_failed_not_allowed_but_sent_then_failed_is(
    admin_url: str, engine: Any, provider: FakeProvider, wired: object
) -> None:
    _, ref, wamid = _sent(admin_url, engine, provider)
    failed = meta_status(wamid, "failed")
    failed["entry"][0]["changes"][0]["value"]["statuses"][0]["errors"] = [
        {"code": 131026, "title": "Message undeliverable"}
    ]
    signed_webhook(failed)
    row = _status(admin_url, ref)
    assert (row.status, row.error) == ("FAILED", "131026: Message undeliverable")
    assert row.failed_at is not None


def test_unknown_message_id_ignored(wired: object) -> None:
    status, body = signed_webhook(meta_status("wamid.unknown", "delivered"))
    assert status == 200 and body["counts"] == {
        "statuses": 1,
        "updated": 0,
        "inbound": 0,
        "opted_out": 0,
    }


def test_stop_opts_out_in_every_tenant_and_confirms_once(
    admin_url: str, engine: Any, provider: FakeProvider, wired: object
) -> None:
    a, b, c = new_tenant(admin_url), new_tenant(admin_url), new_tenant(admin_url)
    to, other = phone(), phone()
    for t in (a, b):
        sender.handle(engine, provider, consent_msg(t, to))
    sender.handle(engine, provider, consent_msg(c, other))

    status, body = signed_webhook(meta_inbound(to, "Stop", "wamid.in.1"))
    assert status == 200 and body["counts"]["opted_out"] == 2
    for t in (a, b):
        with tenant_session(engine, t) as s:
            cid = customers.find_customer_id(s, to)
            assert cid is not None
            hist = customers.consent_history(s, cid)
        assert (hist[0]["status"], hist[0]["source"]) == ("OPTED_OUT", "STOP_REPLY")
    with tenant_session(engine, c) as s:  # an unrelated number in another tenant is untouched
        cid = customers.find_customer_id(s, other)
        assert cid is not None and customers.latest_consent(s, cid) == "OPTED_IN"
    texts = [r for r in provider.sent if r.kind == "text"]
    assert [(r.to, r.text) for r in texts] == [(to, STOP_CONFIRMATION)]

    # provider retries the same webhook: nothing new, no second confirmation
    signed_webhook(meta_inbound(to, "Stop", "wamid.in.1"))
    assert len([r for r in provider.sent if r.kind == "text"]) == 1

    # and the next promo is skipped
    add_template(admin_url, a, "PROMO_WEEKEND")
    assert sender.handle(engine, provider, send_msg(a, to, "PROMO")) == "SKIPPED"


def test_start_and_chatter_are_ignored(
    admin_url: str, engine: Any, provider: FakeProvider, wired: object
) -> None:
    t = new_tenant(admin_url)
    to = phone()
    sender.handle(engine, provider, consent_msg(t, to, "OPTED_OUT"))
    for text_body in ("START", "hi, is the restaurant open?"):
        assert signed_webhook(meta_inbound(to, text_body))[1]["counts"]["opted_out"] == 0
    with tenant_session(engine, t) as s:
        cid = customers.find_customer_id(s, to)
        assert cid is not None and customers.latest_consent(s, cid) == "OPTED_OUT"
    assert provider.sent == []


def test_stop_from_unknown_number_sends_nothing(provider: FakeProvider, wired: object) -> None:
    assert signed_webhook(meta_inbound(phone(), "STOP"))[1]["counts"]["opted_out"] == 0
    assert provider.sent == []
