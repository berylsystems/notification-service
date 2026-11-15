"""Admin reads/writes: message log, customers + consent, templates, per-tenant settings.

Lists use keyset (cursor) pagination on ``(created_at, id)`` newest first; the cursor is opaque
(base64 JSON). Phones are masked in every response.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sg_commons.db import tenant_session
from sg_commons.errors import NotFoundError
from sqlalchemy import Engine, text

from ..domain.enums import ConsentSource, ConsentStatus, mask_phone
from . import customers
from .common import decode_cursor, encode_cursor


def _page(rows: list[dict[str, Any]], limit: int) -> tuple[list[dict[str, Any]], str | None]:
    more = len(rows) > limit
    rows = rows[:limit]
    nxt = encode_cursor(rows[-1]["created_at"], rows[-1]["id"]) if more and rows else None
    return rows, nxt


# ---- messages ----


def list_messages(
    engine: Engine,
    tenant_id: UUID,
    *,
    purpose: list[str] | None = None,
    status: list[str] | None = None,
    outlet_id: UUID | None = None,
    campaign_id: UUID | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    where: list[str] = ["true"]
    params: dict[str, Any] = {"n": limit + 1}
    if purpose:
        where.append("purpose = ANY(:purpose)")
        params["purpose"] = purpose
    if status:
        where.append("status = ANY(:status)")
        params["status"] = status
    if outlet_id:
        where.append("outlet_id = :outlet")
        params["outlet"] = outlet_id
    if campaign_id:
        where.append("campaign_id = :campaign")
        params["campaign"] = campaign_id
    if date_from:
        where.append("created_at >= :from")
        params["from"] = date_from
    if date_to:
        where.append("created_at < :to")
        params["to"] = date_to
    if cursor:
        at, rid = decode_cursor(cursor)
        where.append("(created_at, id) < (:c_at, :c_id)")
        params.update(c_at=at, c_id=rid)
    with tenant_session(engine, tenant_id) as s:
        rows = [
            dict(r)
            for r in s.execute(
                text(
                    f"""
                    SELECT id, outlet_id, customer_id, phone, purpose, template_code, params,
                           ref_type, ref_id, campaign_id, status, skip_reason, provider, error,
                           category, attempts, created_at, sent_at, delivered_at, read_at,
                           failed_at
                    FROM message WHERE {" AND ".join(where)}
                    ORDER BY created_at DESC, id DESC LIMIT :n
                    """
                ),
                params,
            ).mappings()
        ]
    items, nxt = _page(rows, limit)
    for item in items:
        item["phone"] = mask_phone(item["phone"])
    return {"items": items, "next_cursor": nxt}


# ---- customers ----

_CUSTOMER_SELECT = """
    SELECT c.id, c.phone, c.name, c.first_seen_outlet_id, c.last_outlet_id, c.last_visit_at,
           c.visit_count, c.created_at, lc.status AS consent, lc.at AS consent_at
    FROM customer c
    LEFT JOIN LATERAL (
        SELECT status, at FROM customer_consent cc WHERE cc.customer_id = c.id
        ORDER BY cc.id DESC LIMIT 1
    ) lc ON true
"""


def _customer_out(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, "phone": mask_phone(row["phone"])}


def list_customers(
    engine: Engine,
    tenant_id: UUID,
    *,
    consent: str | None = None,
    phone: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    where: list[str] = ["true"]
    params: dict[str, Any] = {"n": limit + 1}
    if consent == "NONE":
        where.append("lc.status IS NULL")
    elif consent:
        where.append("lc.status = :consent")
        params["consent"] = consent
    if phone:
        where.append("c.phone = :phone")
        params["phone"] = phone
    if cursor:
        at, rid = decode_cursor(cursor)
        where.append("(c.created_at, c.id) < (:c_at, :c_id)")
        params.update(c_at=at, c_id=rid)
    with tenant_session(engine, tenant_id) as s:
        rows = [
            dict(r)
            for r in s.execute(
                text(
                    f"{_CUSTOMER_SELECT} WHERE {' AND '.join(where)} "
                    "ORDER BY c.created_at DESC, c.id DESC LIMIT :n"
                ),
                params,
            ).mappings()
        ]
    items, nxt = _page(rows, limit)
    return {"items": [_customer_out(r) for r in items], "next_cursor": nxt}


def get_customer(engine: Engine, tenant_id: UUID, customer_id: UUID) -> dict[str, Any]:
    with tenant_session(engine, tenant_id) as s:
        row = s.execute(text(f"{_CUSTOMER_SELECT} WHERE c.id = :id"), {"id": customer_id})
        found = row.mappings().first()
        if found is None:
            raise NotFoundError("customer not found")
        history = customers.consent_history(s, customer_id)
    return {**_customer_out(dict(found)), "history": history}


def opt_out(
    engine: Engine, tenant_id: UUID, customer_id: UUID, actor_id: UUID, note: str | None
) -> dict[str, Any]:
    with tenant_session(engine, tenant_id) as s:
        if s.execute(text("SELECT 1 FROM customer WHERE id = :id"), {"id": customer_id}).first() \
                is None:  # fmt: skip
            raise NotFoundError("customer not found")
        customers.append_consent(
            s, tenant_id, customer_id, ConsentStatus.OPTED_OUT, ConsentSource.ADMIN.value,
            actor_id=actor_id, note=note,
        )  # fmt: skip
    return get_customer(engine, tenant_id, customer_id)


# ---- templates ----


def list_templates(engine: Engine, tenant_id: UUID) -> list[dict[str, Any]]:
    with tenant_session(engine, tenant_id) as s:
        rows = s.execute(
            text(
                """
                SELECT id, tenant_id, code, provider_template_name, category, language, variables,
                       status, updated_at
                FROM wa_template ORDER BY code, language, tenant_id NULLS LAST
                """
            )
        ).mappings()
        return [
            {**dict(r), "scope": "PLATFORM" if r["tenant_id"] is None else "TENANT"} for r in rows
        ]


def upsert_template(engine: Engine, tenant_id: UUID, data: dict[str, Any]) -> dict[str, Any]:
    """Tenant mapping of a code (+language) to a provider template; overrides a platform row."""
    with tenant_session(engine, tenant_id) as s:
        row = (
            s.execute(
                text(
                    """
                INSERT INTO wa_template (tenant_id, code, provider_template_name, category,
                                         language, variables, status)
                VALUES (:tid, :code, :name, :category, :lang, CAST(:vars AS jsonb), :status)
                ON CONFLICT ((coalesce(tenant_id, '00000000-0000-0000-0000-000000000000'::uuid)),
                             code, language)
                DO UPDATE SET provider_template_name = EXCLUDED.provider_template_name,
                    category = EXCLUDED.category, variables = EXCLUDED.variables,
                    status = EXCLUDED.status, updated_at = now()
                RETURNING id, tenant_id, code, provider_template_name, category, language,
                          variables, status, updated_at
                """
                ),
                {
                    "tid": tenant_id,
                    "code": data["code"],
                    "name": data["provider_template_name"],
                    "category": data["category"],
                    "lang": data["language"],
                    "vars": json.dumps(data.get("variables") or []),
                    "status": data["status"],
                },
            )
            .mappings()
            .one()
        )
        return {**dict(row), "scope": "TENANT"}


# ---- settings ----


def get_settings(engine: Engine, tenant_id: UUID) -> dict[str, Any]:
    with tenant_session(engine, tenant_id) as s:
        row = s.execute(text("SELECT promo_cap_days FROM tenant_notify_settings")).first()
    return {"promo_cap_days": int(row[0]) if row else 7}


def put_settings(engine: Engine, tenant_id: UUID, promo_cap_days: int) -> dict[str, Any]:
    with tenant_session(engine, tenant_id) as s:
        s.execute(
            text(
                "INSERT INTO tenant_notify_settings (tenant_id, promo_cap_days) VALUES (:t, :d) "
                "ON CONFLICT (tenant_id) DO UPDATE SET promo_cap_days = EXCLUDED.promo_cap_days, "
                "updated_at = now()"
            ),
            {"t": tenant_id, "d": promo_cap_days},
        )
    return {"promo_cap_days": promo_cap_days}
