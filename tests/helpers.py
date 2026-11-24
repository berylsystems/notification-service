"""Test helpers: seeding, minting admin tokens, calling the HTTP router, signed webhooks."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any
from uuid import UUID, uuid4

from sg_commons.config import get_settings
from sg_commons.crypto import tokens
from sg_commons.crypto.signer import LocalRsaSigner
from sg_commons.db import make_engine
from sqlalchemy import text

from notification_service.api import app as api

APP_SECRET = "test-app-secret"
VERIFY_TOKEN = "test-verify-token"
ALL_NOTIFY = ["notify:read", "notify:write"]


def provider_config(provider: str = "fake") -> dict[str, str]:
    return {
        "provider": provider,
        "access_token": "test-token",
        "phone_number_id": "1234567890",
        "app_secret": APP_SECRET,
        "verify_token": VERIFY_TOKEN,
        "graph_version": "v21.0",
    }


def admin_exec(admin_url: str, sql: str, **params: object) -> None:
    engine = make_engine(admin_url)
    with engine.begin() as conn:
        conn.execute(text(sql), params)
    engine.dispose()


def admin_fetch(admin_url: str, sql: str, **params: object) -> list[Any]:
    engine = make_engine(admin_url)
    with engine.begin() as conn:
        rows = list(conn.execute(text(sql), params).all())
    engine.dispose()
    return rows


def new_tenant(admin_url: str, *, approve_platform: bool = True) -> UUID:
    tid = uuid4()
    admin_exec(
        admin_url,
        "INSERT INTO tenant (id, slug, name) VALUES (:id, :s, :s)",
        id=tid,
        s=f"t-{tid.hex[:12]}",
    )
    if approve_platform:
        admin_exec(admin_url, "UPDATE wa_template SET status = 'APPROVED' WHERE tenant_id IS NULL")
    return tid


def add_template(
    admin_url: str,
    tenant_id: UUID | None,
    code: str,
    *,
    category: str = "MARKETING",
    status: str = "APPROVED",
    name: str | None = None,
) -> None:
    admin_exec(
        admin_url,
        "INSERT INTO wa_template (tenant_id, code, provider_template_name, category, status) "
        "VALUES (:t, :c, :n, :cat, :st)",
        t=tenant_id,
        c=code,
        n=name or code.lower(),
        cat=category,
        st=status,
    )


def phone() -> str:
    return "+9198" + str(uuid4().int)[:8]


def send_msg(tenant_id: UUID, to: str, purpose: str = "BILL", **extra: Any) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "v": 1,
        "type": "SEND",
        "purpose": purpose,
        "tenant_id": str(tenant_id),
        "outlet_id": None,
        "to": to,
        "template_code": {"BILL": "BILL", "OTP": "OTP", "STAFF": "STAFF_COUNT_DUE"}.get(
            purpose, "PROMO_WEEKEND"
        ),
        "language": "en",
        "params": ["Andheri Foods", "A-0001", "540.00"],
        "buttons": [],
        "ref_type": "pos_ticket",
        "ref_id": str(uuid4()),
    }
    msg.update(extra)
    return msg


def consent_msg(tenant_id: UUID, to: str, status: str = "OPTED_IN", **extra: Any) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "v": 1,
        "type": "CONSENT",
        "tenant_id": str(tenant_id),
        "phone": to,
        "name": "Ravi",
        "status": status,
        "source": "POS",
        "outlet_id": None,
        "actor_id": None,
    }
    msg.update(extra)
    return msg


# ---- tokens ----


def admin_token(signer: LocalRsaSigner, tenant_id: UUID, perms: list[str]) -> str:
    s = get_settings()
    token, _ = tokens.encode_access_token(
        signer,
        subject=str(uuid4()),
        tenant_id=str(tenant_id),
        tenant_slug="x",
        email="boss@x.test",
        issuer=s.jwt_issuer,
        audience=s.jwt_audience,
        ttl_seconds=600,
        extra_claims={"roles": [], "permissions": perms},
    )
    return token


# ---- HTTP ----


def _event(
    method: str,
    path: str,
    body: Any,
    token: str | None,
    headers: dict[str, str] | None,
    raw_body: str | None = None,
) -> dict[str, Any]:
    hdrs = {"content-type": "application/json", **(headers or {})}
    if token:
        hdrs["authorization"] = f"Bearer {token}"
    path, _, query = path.partition("?")
    params = dict(p.split("=", 1) for p in query.split("&") if p)
    if raw_body is None:
        raw_body = json.dumps(body) if body is not None else None
    return {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": path,
        "rawQueryString": query,
        "queryStringParameters": params or None,
        "headers": hdrs,
        "requestContext": {
            "http": {
                "method": method,
                "path": path,
                "sourceIp": "203.0.113.7",
                "protocol": "HTTP/1.1",
                "userAgent": "pytest",
            },
            "stage": "$default",
            "requestId": "t",
            "accountId": "0",
            "apiId": "a",
            "domainName": "d",
            "time": "t",
            "timeEpoch": 0,
        },
        "body": raw_body,
        "isBase64Encoded": False,
    }


def call_full(
    method: str,
    path: str,
    body: Any = None,
    token: str | None = None,
    headers: dict[str, str] | None = None,
    raw_body: str | None = None,
) -> tuple[int, Any, dict[str, Any]]:
    resp = api.app.resolve(_event(method, path, body, token, headers, raw_body), None)  # type: ignore[arg-type]
    raw = resp.get("body") or ""
    ctype = (resp.get("headers") or {}).get("Content-Type", "")
    data = json.loads(raw) if raw and "json" in ctype else raw
    return resp["statusCode"], data, resp.get("headers") or {}


def call(method: str, path: str, body: Any = None, token: str | None = None) -> tuple[int, Any]:
    status, data, _ = call_full(method, path, body, token)
    return status, data


def signed_webhook(payload: dict[str, Any], secret: str = APP_SECRET) -> tuple[int, Any]:
    raw = json.dumps(payload)
    sig = "sha256=" + hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
    status, data, _ = call_full(
        "POST", "/webhooks/whatsapp/fake", headers={"x-hub-signature-256": sig}, raw_body=raw
    )
    return status, data


def meta_status(provider_msg_id: str, status: str, ts: int = 1_763_200_000) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "statuses": [
                                {
                                    "id": provider_msg_id,
                                    "status": status,
                                    "timestamp": str(ts),
                                    "recipient_id": "919800000000",
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def meta_inbound(from_e164: str, body: str, msg_id: str | None = None) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "messages": [
                                {
                                    "from": from_e164.lstrip("+"),
                                    "id": msg_id or f"wamid.in.{uuid4().hex}",
                                    "timestamp": "1763200000",
                                    "type": "text",
                                    "text": {"body": body},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }
