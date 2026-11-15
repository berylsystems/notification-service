"""Campaigns: CRUD (DRAFT only), estimate math, send/schedule -> scheduler fan-out in pages ->
sender counts, cancel, and the audience filters."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from sg_commons.crypto.signer import LocalRsaSigner

from helpers import (
    ALL_NOTIFY,
    add_template,
    admin_exec,
    admin_fetch,
    admin_token,
    call,
    consent_msg,
    new_tenant,
    phone,
)
from notification_service.providers.fake import FakeProvider
from notification_service.services import campaigns, sender
from notification_service.services.queue import ListPublisher


def _audience(
    admin_url: str,
    engine: Any,
    provider: FakeProvider,
    t: UUID,
    n: int,
    status: str = "OPTED_IN",
    outlet: UUID | None = None,
) -> list[str]:
    phones = [phone() for _ in range(n)]
    for p in phones:
        sender.handle(
            engine, provider, consent_msg(t, p, status, outlet_id=str(outlet) if outlet else None)
        )
    return phones


def _create(token: str, **extra: Any) -> dict[str, Any]:
    body = {"name": "Weekend", "template_code": "PROMO_WEEKEND", "params": ["20% off"], **extra}
    status, out = call("POST", "/notify/campaigns", body, token)
    assert status == 201, out
    return out  # type: ignore[no-any-return]


def test_crud_draft_only(admin_url: str, wired: LocalRsaSigner) -> None:
    t = new_tenant(admin_url)
    token = admin_token(wired, t, ALL_NOTIFY)
    c = _create(token, visited_within_days=30)
    assert (c["status"], c["recipients"], c["visited_within_days"]) == ("DRAFT", 0, 30)
    status, out = call(
        "PUT",
        f"/notify/campaigns/{c['id']}",
        {"name": "Diwali", "outlet_ids": [str(uuid4())]},
        token,
    )
    assert status == 200 and out["name"] == "Diwali" and len(out["outlet_ids"]) == 1
    assert out["params"] == ["20% off"]  # untouched fields kept
    assert [x["id"] for x in call("GET", "/notify/campaigns", token=token)[1]] == [c["id"]]
    assert call("GET", f"/notify/campaigns/{c['id']}", token=token)[1]["name"] == "Diwali"
    assert (
        call("POST", "/notify/campaigns", {"name": "x", "template_code": "BILL"}, token)[0] == 422
    )
    call("POST", f"/notify/campaigns/{c['id']}/cancel", None, token)
    assert call("PUT", f"/notify/campaigns/{c['id']}", {"name": "late"}, token)[0] == 409
    other = admin_token(wired, new_tenant(admin_url), ALL_NOTIFY)
    assert call("GET", f"/notify/campaigns/{c['id']}", token=other)[0] == 404


def test_estimate(
    admin_url: str, engine: Any, provider: FakeProvider, wired: LocalRsaSigner
) -> None:
    t = new_tenant(admin_url)
    add_template(admin_url, t, "PROMO_WEEKEND")
    _audience(admin_url, engine, provider, t, 3)
    _audience(admin_url, engine, provider, t, 2, "OPTED_OUT")
    token = admin_token(wired, t, ALL_NOTIFY)
    c = _create(token)
    status, est = call("POST", f"/notify/campaigns/{c['id']}/estimate", None, token)
    assert status == 200
    assert est["recipients"] == 3 and est["category"] == "MARKETING"
    assert Decimal(est["subtotal_paise"]) == Decimal("258.93")  # 3 x 86.31
    assert Decimal(est["gst_paise"]) == Decimal("46.61")  # 18%, half-up
    assert Decimal(est["total_paise"]) == Decimal("305.54")
    assert call("GET", f"/notify/campaigns/{c['id']}", token=token)[1]["est_cost_paise"] == 306


def test_estimate_with_markup(
    admin_url: str,
    engine: Any,
    provider: FakeProvider,
    wired: LocalRsaSigner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from notification_service import config

    t = new_tenant(admin_url)
    _audience(admin_url, engine, provider, t, 10)
    token = admin_token(wired, t, ALL_NOTIFY)
    c = _create(token)
    monkeypatch.setenv("SG_NOTIFY_PROVIDER_MARKUP_PAISE", "9")
    config.get_notify_settings.cache_clear()
    try:
        _, est = call("POST", f"/notify/campaigns/{c['id']}/estimate", None, token)
    finally:
        config.get_notify_settings.cache_clear()
    assert Decimal(est["subtotal_paise"]) == Decimal("953.10")  # 10 x (86.31 + 9)
    assert Decimal(est["total_paise"]) == Decimal("1124.66")


def test_send_requires_approved_marketing_template(admin_url: str, wired: LocalRsaSigner) -> None:
    t = new_tenant(admin_url)
    token = admin_token(wired, t, ALL_NOTIFY)
    c = _create(token)
    assert call("POST", f"/notify/campaigns/{c['id']}/send", {}, token)[0] == 422  # not mapped
    add_template(admin_url, t, "PROMO_WEEKEND", status="PENDING")
    assert call("POST", f"/notify/campaigns/{c['id']}/send", {}, token)[0] == 422
    admin_exec(
        admin_url,
        "UPDATE wa_template SET status = 'APPROVED', category = 'UTILITY' WHERE tenant_id = :t",
        t=t,
    )
    assert call("POST", f"/notify/campaigns/{c['id']}/send", {}, token)[0] == 422
    admin_exec(admin_url, "UPDATE wa_template SET category = 'MARKETING' WHERE tenant_id = :t", t=t)
    status, out = call("POST", f"/notify/campaigns/{c['id']}/send", {}, token)
    assert status == 200 and out["status"] == "SCHEDULED"
    assert call("POST", f"/notify/campaigns/{c['id']}/send", {}, token)[0] == 409


def test_fan_out_pages_and_sender_counts(
    admin_url: str, engine: Any, provider: FakeProvider, wired: LocalRsaSigner
) -> None:
    t = new_tenant(admin_url)
    add_template(admin_url, t, "PROMO_WEEKEND")
    phones = _audience(admin_url, engine, provider, t, 7)
    _audience(admin_url, engine, provider, t, 2, "OPTED_OUT")
    token = admin_token(wired, t, ALL_NOTIFY)
    c = _create(token, buttons=[{"type": "URL", "value": "weekend"}])
    # one recipient already got a promo this week -> frequency cap
    capped = phones[0]
    sender.handle(
        engine,
        provider,
        {
            "v": 1,
            "type": "SEND",
            "purpose": "PROMO",
            "tenant_id": str(t),
            "to": capped,
            "template_code": "PROMO_WEEKEND",
            "params": ["x"],
            "ref_id": "earlier",
        },
    )
    provider.sent.clear()
    call("POST", f"/notify/campaigns/{c['id']}/send", {}, token)

    pub = ListPublisher()
    out = campaigns.run_due(engine, pub, page_size=3)
    assert out[c["id"]] == 7
    pub.messages = [m for m in pub.messages if m["campaign_id"] == c["id"]]
    assert len(pub.messages) == 7
    m0 = pub.messages[0]
    assert (m0["type"], m0["purpose"], m0["campaign_id"], m0["ref_type"]) == (
        "SEND",
        "PROMO",
        c["id"],
        "campaign",
    )
    assert m0["ref_id"].startswith(f"{c['id']}:")
    assert c["id"] not in campaigns.run_due(engine, pub)  # not due any more
    _, got = call("GET", f"/notify/campaigns/{c['id']}", token=token)
    assert (got["status"], got["recipients"]) == ("DONE", 7)

    for m in pub.messages + pub.messages[:2]:  # includes 2 duplicate deliveries
        sender.handle(engine, provider, m)
    _, got = call("GET", f"/notify/campaigns/{c['id']}", token=token)
    assert (got["sent"], got["skipped"], got["failed"]) == (6, 1, 0)
    assert len(provider.sent) == 6 and provider.sent[0].buttons[0].value == "weekend"


def test_scheduled_in_future_waits_and_cancel(
    admin_url: str, engine: Any, provider: FakeProvider, wired: LocalRsaSigner
) -> None:
    t = new_tenant(admin_url)
    add_template(admin_url, t, "PROMO_WEEKEND")
    _audience(admin_url, engine, provider, t, 2)
    token = admin_token(wired, t, ALL_NOTIFY)
    c = _create(token)
    later = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    status, out = call("POST", f"/notify/campaigns/{c['id']}/send", {"scheduled_at": later}, token)
    assert status == 200 and out["status"] == "SCHEDULED"
    pub = ListPublisher()
    assert c["id"] not in campaigns.run_due(engine, pub)
    status, out = call("POST", f"/notify/campaigns/{c['id']}/cancel", None, token)
    assert status == 200 and out["status"] == "CANCELLED"
    assert (
        admin_fetch(
            admin_url, "SELECT count(*) FROM campaign_due WHERE campaign_id = :c", c=c["id"]
        )[0][0]
        == 0
    )
    assert call("POST", f"/notify/campaigns/{c['id']}/cancel", None, token)[0] == 409


def test_cancel_while_sending_skips_queued_messages(
    admin_url: str, engine: Any, provider: FakeProvider, wired: LocalRsaSigner
) -> None:
    t = new_tenant(admin_url)
    add_template(admin_url, t, "PROMO_WEEKEND")
    _audience(admin_url, engine, provider, t, 3)
    token = admin_token(wired, t, ALL_NOTIFY)
    c = _create(token)
    call("POST", f"/notify/campaigns/{c['id']}/send", {}, token)
    pub = ListPublisher()
    campaigns.run_due(engine, pub)
    pub.messages = [m for m in pub.messages if m["campaign_id"] == c["id"]]
    admin_exec(admin_url, "UPDATE campaign SET status = 'SENDING' WHERE id = :c", c=c["id"])
    call("POST", f"/notify/campaigns/{c['id']}/cancel", None, token)
    for m in pub.messages:
        assert sender.handle(engine, provider, m) == "SKIPPED"
    _, got = call("GET", f"/notify/campaigns/{c['id']}", token=token)
    assert (got["status"], got["skipped"]) == ("CANCELLED", 3)


def test_audience_filters(
    admin_url: str, engine: Any, provider: FakeProvider, wired: LocalRsaSigner
) -> None:
    t = new_tenant(admin_url)
    a, b = uuid4(), uuid4()
    in_a = _audience(admin_url, engine, provider, t, 2, outlet=a)
    _audience(admin_url, engine, provider, t, 3, outlet=b)
    admin_exec(
        admin_url,
        "UPDATE customer SET last_visit_at = now() - interval '40 days' WHERE tenant_id = :t",
        t=t,
    )
    admin_exec(admin_url, "UPDATE customer SET last_visit_at = now() WHERE phone = :p", p=in_a[0])
    token = admin_token(wired, t, ALL_NOTIFY)
    by_outlet = _create(token, outlet_ids=[str(a)])
    recent = _create(token, visited_within_days=30)
    both = _create(token, outlet_ids=[str(b)], visited_within_days=30)

    def est(cid: str) -> int:
        return int(call("POST", f"/notify/campaigns/{cid}/estimate", None, token)[1]["recipients"])

    assert (est(by_outlet["id"]), est(recent["id"]), est(both["id"])) == (2, 1, 0)
