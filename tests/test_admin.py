"""Admin routes: permissions and token audience, message log (masked, filtered, cursor-paged),
customers + consent history + admin opt-out, templates, settings, RFC 7807 errors."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from sg_commons.crypto.signer import LocalRsaSigner

from helpers import (
    ALL_NOTIFY,
    admin_token,
    call,
    consent_msg,
    new_tenant,
    phone,
    send_msg,
)
from notification_service.providers.fake import FakeProvider
from notification_service.services import sender

_READ_ROUTES = [
    ("GET", "/notify/messages"),
    ("GET", "/notify/customers"),
    ("GET", f"/notify/customers/{uuid4()}"),
    ("GET", "/notify/templates"),
    ("GET", "/notify/settings"),
    ("GET", "/notify/campaigns"),
    ("GET", f"/notify/campaigns/{uuid4()}"),
    ("POST", f"/notify/campaigns/{uuid4()}/estimate"),
]
_WRITE_ROUTES = [
    ("POST", f"/notify/customers/{uuid4()}/opt-out"),
    ("PUT", "/notify/templates"),
    ("PUT", "/notify/settings"),
    ("POST", "/notify/campaigns"),
    ("PUT", f"/notify/campaigns/{uuid4()}"),
    ("POST", f"/notify/campaigns/{uuid4()}/send"),
    ("POST", f"/notify/campaigns/{uuid4()}/cancel"),
]


@pytest.mark.parametrize(("method", "path"), _READ_ROUTES + _WRITE_ROUTES)
def test_requires_token(method: str, path: str, wired: LocalRsaSigner) -> None:
    status, body = call(method, path, {})
    assert status == 401 and body["status"] == 401


@pytest.mark.parametrize(("method", "path"), _READ_ROUTES)
def test_read_routes_need_notify_read(
    method: str, path: str, admin_url: str, wired: LocalRsaSigner
) -> None:
    token = admin_token(wired, new_tenant(admin_url), ["order:read", "notify:write"])
    status, body = call(method, path, {}, token)
    assert status == 403 and body["detail"] == "missing permission: notify:read"


@pytest.mark.parametrize(("method", "path"), _WRITE_ROUTES)
def test_write_routes_need_notify_write(
    method: str, path: str, admin_url: str, wired: LocalRsaSigner
) -> None:
    token = admin_token(wired, new_tenant(admin_url), ["notify:read"])
    assert call(method, path, {}, token)[0] == 403


def test_message_log_masked_filtered_paged(
    admin_url: str, engine: Any, provider: FakeProvider, wired: LocalRsaSigner
) -> None:
    t, other = new_tenant(admin_url), new_tenant(admin_url)
    outlet = uuid4()
    to = phone()
    for _ in range(3):
        sender.handle(engine, provider, send_msg(t, to, "BILL", outlet_id=str(outlet)))
    sender.handle(engine, provider, send_msg(t, to, "OTP", params=["999999"]))
    sender.handle(engine, provider, send_msg(other, phone(), "BILL"))
    token = admin_token(wired, t, ["notify:read"])

    status, body = call("GET", "/notify/messages", token=token)
    assert status == 200 and len(body["items"]) == 4  # the other tenant's message is invisible
    assert {m["phone"] for m in body["items"]} == {to[:3] + "******" + to[-4:]}
    otp = next(m for m in body["items"] if m["purpose"] == "OTP")
    assert otp["params"] == ["******"]

    status, body = call(
        "GET", f"/notify/messages?purpose=BILL&outlet_id={outlet}&limit=2", token=token
    )
    assert len(body["items"]) == 2 and body["next_cursor"]
    _, page2 = call(
        "GET", f"/notify/messages?purpose=BILL&limit=2&cursor={body['next_cursor']}", token=token
    )
    assert len(page2["items"]) == 1 and page2["next_cursor"] is None
    ids = {m["id"] for m in body["items"]} | {m["id"] for m in page2["items"]}
    assert len(ids) == 3

    assert call("GET", "/notify/messages?status=SENT,READ", token=token)[1]["items"]
    assert call("GET", "/notify/messages?status=NOPE", token=token)[0] == 422
    assert call("GET", "/notify/messages?cursor=garbage", token=token)[0] == 422
    assert call("GET", "/notify/messages?from=2099-01-01", token=token)[1]["items"] == []
    assert len(call("GET", "/notify/messages?to=2099-01-01", token=token)[1]["items"]) == 4


def test_customers_history_and_admin_opt_out(
    admin_url: str, engine: Any, provider: FakeProvider, wired: LocalRsaSigner
) -> None:
    t = new_tenant(admin_url)
    to, never = phone(), phone()
    sender.handle(engine, provider, consent_msg(t, to))
    sender.handle(engine, provider, send_msg(t, never, "BILL"))
    token = admin_token(wired, t, ALL_NOTIFY)

    _, body = call("GET", "/notify/customers", token=token)
    assert len(body["items"]) == 2
    _, opted = call("GET", "/notify/customers?consent=OPTED_IN", token=token)
    assert [c["consent"] for c in opted["items"]] == ["OPTED_IN"]
    _, none = call("GET", "/notify/customers?consent=NONE", token=token)
    assert none["items"][0]["visit_count"] == 1
    _, by_phone = call("GET", f"/notify/customers?phone={to[1:]}", token=token)
    assert len(by_phone["items"]) == 1
    cid = opted["items"][0]["id"]

    status, detail = call(
        "POST", f"/notify/customers/{cid}/opt-out", {"note": "asked by phone"}, token
    )
    assert status == 200 and detail["consent"] == "OPTED_OUT"
    assert [(h["status"], h["source"]) for h in detail["history"]] == [
        ("OPTED_OUT", "ADMIN"),
        ("OPTED_IN", "POS"),
    ]
    assert detail["history"][0]["actor_id"] is not None
    assert call("GET", f"/notify/customers/{uuid4()}", token=token)[0] == 404
    other = admin_token(wired, new_tenant(admin_url), ALL_NOTIFY)
    assert call("GET", f"/notify/customers/{cid}", token=other)[0] == 404  # RLS


def test_templates_and_settings(admin_url: str, wired: LocalRsaSigner) -> None:
    t = new_tenant(admin_url)
    token = admin_token(wired, t, ALL_NOTIFY)
    _, rows = call("GET", "/notify/templates", token=token)
    assert {(r["code"], r["scope"]) for r in rows} >= {("BILL", "PLATFORM"), ("OTP", "PLATFORM")}
    body = {
        "code": "PROMO_WEEKEND",
        "provider_template_name": "acme_weekend_v1",
        "category": "MARKETING",
        "variables": ["store", "offer"],
        "status": "APPROVED",
    }
    status, out = call("PUT", "/notify/templates", body, token)
    assert status == 200 and out["scope"] == "TENANT" and out["language"] == "en"
    status, out = call("PUT", "/notify/templates", {**body, "status": "REJECTED"}, token)
    assert out["status"] == "REJECTED"
    _, rows = call("GET", "/notify/templates", token=token)
    assert sum(1 for r in rows if r["code"] == "PROMO_WEEKEND") == 1
    assert call("PUT", "/notify/templates", {**body, "code": "promo"}, token)[0] == 422
    # platform templates on the shared number can't be overridden per tenant
    assert call("PUT", "/notify/templates", {**body, "code": "BILL"}, token)[0] == 422
    assert call("PUT", "/notify/templates", {**body, "code": "OTP"}, token)[0] == 422
    assert call("PUT", "/notify/templates", {**body, "category": "UTILITY"}, token)[0] == 422

    assert call("GET", "/notify/settings", token=token)[1] == {"promo_cap_days": 7}
    assert call("PUT", "/notify/settings", {"promo_cap_days": 3}, token)[1] == {"promo_cap_days": 3}
    assert call("GET", "/notify/settings", token=token)[1] == {"promo_cap_days": 3}
    status, problem = call("PUT", "/notify/settings", {"promo_cap_days": 999}, token)
    assert status == 422 and problem["type"].endswith("/validation")
