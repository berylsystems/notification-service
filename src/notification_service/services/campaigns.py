"""Promotional campaigns: CRUD (DRAFT only), audience, estimate, send / schedule, cancel and the
scheduler's fan-out.

**Audience** = the tenant's customers whose latest consent is OPTED_IN, optionally limited to
outlets (first-seen or last-visited outlet in ``outlet_ids``) and to a visit within
``visited_within_days``. Consent and the frequency cap are checked again by the sender at send
time, so the audience is only a candidate list.

**Scheduling** goes through the non-RLS ``campaign_due`` table (campaign id, tenant id, when,
lease) because the once-a-minute scheduler has no tenant context. The scheduler leases due rows
for 5 minutes (``FOR UPDATE SKIP LOCKED``), marks the campaign SENDING, enqueues PROMO SEND
messages in pages of 500 with ``ref_id = "<campaign_id>:<customer_id>"`` (so a crashed or
repeated fan-out never double-sends: the sender's idempotency key absorbs it), then marks it DONE
and deletes the due row. ``sent`` / ``failed`` / ``skipped`` are counted by the sender.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from aws_lambda_powertools import Logger
from sg_commons.db import admin_session, tenant_session
from sg_commons.errors import ConflictError, NotFoundError, ValidationError
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from ..domain.enums import (
    CampaignStatus,
    Purpose,
    TemplateCategory,
    TemplateStatus,
    estimate_cost,
)
from .queue import Publisher

logger = Logger(service="notification-service", child=True)

_COLUMNS = """id, name, template_code, language, params, buttons, outlet_ids, visited_within_days,
    scheduled_at, status, recipients, sent, failed, skipped, est_cost_paise, created_by,
    created_at, updated_at"""
_EDITABLE = ("name", "template_code", "language", "params", "buttons", "outlet_ids",
             "visited_within_days", "scheduled_at")  # fmt: skip
_JSON_FIELDS = {"params", "buttons", "outlet_ids"}
LEASE = "5 minutes"


def _get(s: Session, campaign_id: UUID, *, lock: bool = False) -> dict[str, Any]:
    row = (
        s.execute(
            text(f"SELECT {_COLUMNS} FROM campaign WHERE id = :id{' FOR UPDATE' if lock else ''}"),
            {"id": campaign_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFoundError("campaign not found")
    return dict(row)


def _jsonable(field: str, value: Any) -> Any:
    if field == "outlet_ids" and value is not None:
        return json.dumps([str(v) for v in value])
    return json.dumps(value) if field in _JSON_FIELDS else value


def list_campaigns(engine: Engine, tenant_id: UUID, limit: int = 200) -> list[dict[str, Any]]:
    with tenant_session(engine, tenant_id) as s:
        rows = s.execute(
            text(f"SELECT {_COLUMNS} FROM campaign ORDER BY created_at DESC, id DESC LIMIT :n"),
            {"n": limit},
        ).mappings()
        return [dict(r) for r in rows]


def get_campaign(engine: Engine, tenant_id: UUID, campaign_id: UUID) -> dict[str, Any]:
    with tenant_session(engine, tenant_id) as s:
        return _get(s, campaign_id)


def create_campaign(
    engine: Engine, tenant_id: UUID, actor_id: UUID, data: dict[str, Any]
) -> dict[str, Any]:
    fields = {k: data[k] for k in _EDITABLE if k in data}
    names = ", ".join(["tenant_id", "created_by", *fields])
    values = ", ".join(
        [":tenant_id", ":created_by"]
        + [f"CAST(:{k} AS jsonb)" if k in _JSON_FIELDS else f":{k}" for k in fields]
    )
    params = {k: _jsonable(k, v) for k, v in fields.items()}
    with tenant_session(engine, tenant_id) as s:
        row = s.execute(
            text(f"INSERT INTO campaign ({names}) VALUES ({values}) RETURNING id"),
            {"tenant_id": tenant_id, "created_by": actor_id, **params},
        ).one()
        return _get(s, row[0])


def update_campaign(
    engine: Engine, tenant_id: UUID, campaign_id: UUID, data: dict[str, Any]
) -> dict[str, Any]:
    fields = {k: data[k] for k in _EDITABLE if k in data}
    with tenant_session(engine, tenant_id) as s:
        current = _get(s, campaign_id, lock=True)
        if current["status"] != CampaignStatus.DRAFT:
            raise ConflictError("only a DRAFT campaign can be edited")
        if fields:
            sets = ", ".join(
                f"{k} = CAST(:{k} AS jsonb)" if k in _JSON_FIELDS else f"{k} = :{k}" for k in fields
            )
            s.execute(
                text(f"UPDATE campaign SET {sets}, est_cost_paise = NULL, updated_at = now() "
                     "WHERE id = :id"),
                {"id": campaign_id, **{k: _jsonable(k, v) for k, v in fields.items()}},
            )  # fmt: skip
        return _get(s, campaign_id)


# ---- audience ----


def _audience_sql(campaign: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    where = ["lc.status = 'OPTED_IN'"]
    params: dict[str, Any] = {}
    outlets = campaign.get("outlet_ids")
    if outlets:
        where.append("(c.last_outlet_id = ANY(:outlets) OR c.first_seen_outlet_id = ANY(:outlets))")
        params["outlets"] = [UUID(str(o)) for o in outlets]
    days = campaign.get("visited_within_days")
    if days:
        where.append("c.last_visit_at >= now() - make_interval(days => :days)")
        params["days"] = int(days)
    sql = f"""
        FROM customer c
        JOIN LATERAL (
            SELECT status FROM customer_consent cc WHERE cc.customer_id = c.id
            ORDER BY cc.id DESC LIMIT 1
        ) lc ON true
        WHERE {" AND ".join(where)}
    """
    return sql, params


def count_audience(s: Session, campaign: dict[str, Any]) -> int:
    sql, params = _audience_sql(campaign)
    return int(s.execute(text(f"SELECT count(*) {sql}"), params).scalar_one())


def audience_page(
    s: Session, campaign: dict[str, Any], after: UUID | None, size: int
) -> list[tuple[UUID, str]]:
    sql, params = _audience_sql(campaign)
    keyset = " AND c.id > :after" if after else ""
    rows = s.execute(
        text(f"SELECT c.id, c.phone {sql}{keyset} ORDER BY c.id LIMIT :size"),
        {**params, "after": after, "size": size},
    ).all()
    return [(r[0], r[1]) for r in rows]


def _template(s: Session, code: str, language: str) -> dict[str, Any] | None:
    row = (
        s.execute(
            text(
                "SELECT code, category, status FROM wa_template WHERE code = :c AND language = :l "
                "ORDER BY tenant_id NULLS LAST LIMIT 1"
            ),
            {"c": code, "l": language},
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def estimate(
    engine: Engine, tenant_id: UUID, campaign_id: UUID, markup_paise: Decimal
) -> dict[str, Any]:
    with tenant_session(engine, tenant_id) as s:
        campaign = _get(s, campaign_id)
        template = _template(s, campaign["template_code"], campaign["language"])
        category = template["category"] if template else TemplateCategory.MARKETING.value
        recipients = count_audience(s, campaign)
        out = estimate_cost(recipients, category, markup_paise)
        total = Decimal(out["total_paise"])
        if campaign["status"] == CampaignStatus.DRAFT:
            s.execute(
                text("UPDATE campaign SET est_cost_paise = :c WHERE id = :id"),
                {"c": int(total.to_integral_value()), "id": campaign_id},
            )
    return {"campaign_id": campaign_id, "category": category, **out}


def send_campaign(
    engine: Engine, tenant_id: UUID, campaign_id: UUID, scheduled_at: datetime | None
) -> dict[str, Any]:
    """DRAFT -> SCHEDULED (``scheduled_at`` or now); the scheduler picks it up within a minute."""
    with tenant_session(engine, tenant_id) as s:
        campaign = _get(s, campaign_id, lock=True)
        if campaign["status"] != CampaignStatus.DRAFT:
            raise ConflictError(f"campaign is {campaign['status']}")
        if not str(campaign["template_code"]).startswith("PROMO_"):
            raise ValidationError("a campaign needs a PROMO_* template")
        template = _template(s, campaign["template_code"], campaign["language"])
        if template is None:
            raise ValidationError("template not mapped (PUT /notify/templates)")
        if template["status"] != TemplateStatus.APPROVED:
            raise ValidationError(f"template is {template['status']}, not APPROVED")
        if template["category"] != TemplateCategory.MARKETING:
            raise ValidationError("a promotional campaign needs a MARKETING template")
        row = s.execute(
            text(
                "UPDATE campaign SET status = 'SCHEDULED', scheduled_at = coalesce(:at, now()), "
                "updated_at = now() WHERE id = :id RETURNING scheduled_at"
            ),
            {"at": scheduled_at, "id": campaign_id},
        ).one()
        s.execute(
            text(
                "INSERT INTO campaign_due (campaign_id, tenant_id, scheduled_at) "
                "VALUES (:id, :tid, :at) ON CONFLICT (campaign_id) DO UPDATE "
                "SET scheduled_at = EXCLUDED.scheduled_at, locked_until = NULL"
            ),
            {"id": campaign_id, "tid": tenant_id, "at": row[0]},
        )
        return _get(s, campaign_id)


def cancel_campaign(engine: Engine, tenant_id: UUID, campaign_id: UUID) -> dict[str, Any]:
    """DRAFT / SCHEDULED / SENDING -> CANCELLED. Messages already queued are skipped by the
    sender (``CAMPAIGN_CANCELLED``)."""
    with tenant_session(engine, tenant_id) as s:
        campaign = _get(s, campaign_id, lock=True)
        if campaign["status"] in (CampaignStatus.DONE, CampaignStatus.CANCELLED):
            raise ConflictError(f"campaign is {campaign['status']}")
        s.execute(
            text("UPDATE campaign SET status = 'CANCELLED', updated_at = now() WHERE id = :id"),
            {"id": campaign_id},
        )
        s.execute(text("DELETE FROM campaign_due WHERE campaign_id = :id"), {"id": campaign_id})
        return _get(s, campaign_id)


# ---- scheduler ----


def _claim_due(engine: Engine, limit: int) -> list[tuple[UUID, UUID]]:
    with admin_session(engine) as s:
        rows = s.execute(
            text(
                f"""
                UPDATE campaign_due SET locked_until = now() + interval '{LEASE}'
                WHERE campaign_id IN (
                    SELECT campaign_id FROM campaign_due
                    WHERE scheduled_at <= now()
                      AND (locked_until IS NULL OR locked_until < now())
                    ORDER BY scheduled_at LIMIT :n FOR UPDATE SKIP LOCKED
                )
                RETURNING campaign_id, tenant_id
                """
            ),
            {"n": limit},
        ).all()
    return [(r[0], r[1]) for r in rows]


def _send_message(campaign: dict[str, Any], tenant_id: UUID, customer_id: UUID, phone: str
                  ) -> dict[str, Any]:  # fmt: skip
    return {
        "v": 1,
        "type": "SEND",
        "purpose": Purpose.PROMO.value,
        "tenant_id": str(tenant_id),
        "outlet_id": None,
        "to": phone,
        "template_code": campaign["template_code"],
        "language": campaign["language"],
        "params": campaign["params"],
        "buttons": campaign["buttons"],
        "ref_type": "campaign",
        "ref_id": f"{campaign['id']}:{customer_id}",
        "campaign_id": str(campaign["id"]),
    }


def fan_out(
    engine: Engine, publisher: Publisher, campaign_id: UUID, tenant_id: UUID, page_size: int
) -> int | None:
    """Enqueue one due campaign. Returns the recipient count, or None if it was not runnable."""
    with tenant_session(engine, tenant_id) as s:
        campaign = _get(s, campaign_id, lock=True)
        if campaign["status"] not in (CampaignStatus.SCHEDULED, CampaignStatus.SENDING):
            s.execute(text("DELETE FROM campaign_due WHERE campaign_id = :id"), {"id": campaign_id})
            return None
        s.execute(
            text("UPDATE campaign SET status = 'SENDING', updated_at = now() WHERE id = :id"),
            {"id": campaign_id},
        )

    total, after = 0, None
    while True:
        with tenant_session(engine, tenant_id) as s:
            status = s.execute(
                text("SELECT status FROM campaign WHERE id = :id"), {"id": campaign_id}
            ).scalar_one()
            if status == CampaignStatus.CANCELLED:
                break
            page = audience_page(s, campaign, after, page_size)
        if not page:
            break
        publisher.publish([_send_message(campaign, tenant_id, cid, phone) for cid, phone in page])
        total += len(page)
        after = page[-1][0]

    with tenant_session(engine, tenant_id) as s:
        s.execute(
            text(
                "UPDATE campaign SET recipients = :n, updated_at = now(), status = CASE "
                "WHEN status = 'SENDING' THEN 'DONE' ELSE status END WHERE id = :id"
            ),
            {"n": total, "id": campaign_id},
        )
        s.execute(text("DELETE FROM campaign_due WHERE campaign_id = :id"), {"id": campaign_id})
    logger.info("campaign fanned out", extra={"campaign_id": str(campaign_id), "recipients": total})
    return total


def run_due(engine: Engine, publisher: Publisher, page_size: int = 500, limit: int = 10
            ) -> dict[str, int | None]:  # fmt: skip
    """The scheduler tick: fan out every due campaign (bounded per tick)."""
    out: dict[str, int | None] = {}
    for campaign_id, tenant_id in _claim_due(engine, limit):
        out[str(campaign_id)] = fan_out(engine, publisher, campaign_id, tenant_id, page_size)
    return out
