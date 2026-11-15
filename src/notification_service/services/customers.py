"""Customers and their append-only WhatsApp marketing consent.

All functions take a tenant-scoped session (RLS). The current consent of a customer is the latest
``customer_consent`` row (highest id). :func:`tenants_for_phone` is the one cross-tenant read:
the non-RLS ``customer_phone_ref`` index (sha256 of the phone -> tenant / customer ids), used only
by the webhook's STOP handling.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..domain.enums import ConsentStatus


def phone_hash(phone: str) -> str:
    return hashlib.sha256(phone.encode()).hexdigest()


def find_customer_id(session: Session, phone: str) -> UUID | None:
    row = session.execute(text("SELECT id FROM customer WHERE phone = :p"), {"p": phone}).first()
    return row[0] if row else None


def upsert_customer(
    session: Session,
    tenant_id: UUID,
    phone: str,
    *,
    name: str | None = None,
    outlet_id: UUID | None = None,
    visit_at: datetime | None = None,
) -> UUID:
    """Create or update the customer. ``visit_at`` counts a visit (BILL messages)."""
    visit = visit_at is not None
    row = session.execute(
        text(
            """
            INSERT INTO customer (tenant_id, phone, name, first_seen_outlet_id, last_outlet_id,
                                  last_visit_at, visit_count)
            VALUES (:tid, :phone, :name, :outlet, :outlet, :visit_at, :visits)
            ON CONFLICT (tenant_id, phone) DO UPDATE SET
                name = coalesce(EXCLUDED.name, customer.name),
                first_seen_outlet_id = coalesce(customer.first_seen_outlet_id,
                                                EXCLUDED.first_seen_outlet_id),
                last_outlet_id = CASE WHEN :visit THEN coalesce(EXCLUDED.last_outlet_id,
                                      customer.last_outlet_id) ELSE customer.last_outlet_id END,
                last_visit_at = CASE WHEN :visit THEN EXCLUDED.last_visit_at
                                     ELSE customer.last_visit_at END,
                visit_count = customer.visit_count + CASE WHEN :visit THEN 1 ELSE 0 END
            RETURNING id
            """
        ),
        {
            "tid": tenant_id,
            "phone": phone,
            "name": name,
            "outlet": outlet_id,
            "visit_at": visit_at,
            "visits": 1 if visit else 0,
            "visit": visit,
        },
    ).one()
    customer_id: UUID = row[0]
    return customer_id


def latest_consent(session: Session, customer_id: UUID) -> str | None:
    row = session.execute(
        text("SELECT status FROM customer_consent WHERE customer_id = :c ORDER BY id DESC LIMIT 1"),
        {"c": customer_id},
    ).first()
    return str(row[0]) if row else None


def append_consent(
    session: Session,
    tenant_id: UUID,
    customer_id: UUID,
    status: ConsentStatus,
    source: str,
    *,
    outlet_id: UUID | None = None,
    actor_id: UUID | None = None,
    note: str | None = None,
    only_if_changed: bool = True,
) -> bool:
    """Append a consent row; with ``only_if_changed`` a repeat of the current state is a no-op
    (keeps SQS redeliveries and repeated STOPs from piling up rows). Returns True if appended."""
    if only_if_changed and latest_consent(session, customer_id) == status.value:
        return False
    session.execute(
        text(
            """
            INSERT INTO customer_consent (customer_id, tenant_id, status, source, outlet_id,
                                          actor_id, note)
            VALUES (:c, :tid, :status, :source, :outlet, :actor, :note)
            """
        ),
        {
            "c": customer_id,
            "tid": tenant_id,
            "status": status.value,
            "source": source,
            "outlet": outlet_id,
            "actor": actor_id,
            "note": note,
        },
    )
    return True


def tenants_for_phone(session: Session, phone: str) -> list[tuple[UUID, UUID]]:
    """(tenant_id, customer_id) for every tenant where this phone is a customer (no RLS)."""
    rows = session.execute(
        text("SELECT tenant_id, customer_id FROM customer_phone_ref WHERE phone_hash = :h"),
        {"h": phone_hash(phone)},
    ).all()
    return [(r[0], r[1]) for r in rows]


def consent_history(session: Session, customer_id: UUID) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            """
            SELECT id, status, source, outlet_id, actor_id, note, at
            FROM customer_consent WHERE customer_id = :c ORDER BY id DESC
            """
        ),
        {"c": customer_id},
    ).mappings()
    return [dict(r) for r in rows]
