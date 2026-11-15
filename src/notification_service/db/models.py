"""SQLAlchemy ORM models for the notification service.

Kept in sync by hand with the Alembic migrations (which also define RLS, CHECKs, the append-only
trigger on ``customer_consent`` and the trigger that maintains ``customer_phone_ref``). The
services mostly use plain SQL against these tables; the models register them on the metadata and
document the shape. ``tenant`` (restaurant-mgmt) is referenced by FK only and has no model here.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sg_commons.db import Base
from sqlalchemy import BigInteger, DateTime, Identity, Integer, String, text
from sqlalchemy import func as sa_func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

_TS = DateTime(timezone=True)


def _pk_uuid() -> Mapped[uuid.UUID]:
    return mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )


def _uuid_col() -> Mapped[uuid.UUID]:
    return mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)


def _opt_uuid() -> Mapped[uuid.UUID | None]:
    return mapped_column(PgUUID(as_uuid=True))


def _now() -> Mapped[datetime]:
    return mapped_column(_TS, server_default=sa_func.now(), nullable=False)


def _count() -> Mapped[int]:
    return mapped_column(Integer, nullable=False, server_default="0")


# ---- Tenant-scoped (RLS) ----


class Customer(Base):
    __tablename__ = "customer"

    id: Mapped[uuid.UUID] = _pk_uuid()
    tenant_id: Mapped[uuid.UUID] = _uuid_col()
    phone: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str | None] = mapped_column(String(120))
    first_seen_outlet_id: Mapped[uuid.UUID | None] = _opt_uuid()
    last_outlet_id: Mapped[uuid.UUID | None] = _opt_uuid()
    last_visit_at: Mapped[datetime | None] = mapped_column(_TS)
    visit_count: Mapped[int] = _count()
    created_at: Mapped[datetime] = _now()


class CustomerConsent(Base):
    """Append-only; the latest row (highest id) per customer is the current state."""

    __tablename__ = "customer_consent"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    tenant_id: Mapped[uuid.UUID] = _uuid_col()
    channel: Mapped[str] = mapped_column(String(16), nullable=False, server_default="WHATSAPP")
    purpose: Mapped[str] = mapped_column(String(16), nullable=False, server_default="MARKETING")
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    outlet_id: Mapped[uuid.UUID | None] = _opt_uuid()
    actor_id: Mapped[uuid.UUID | None] = _opt_uuid()
    note: Mapped[str | None] = mapped_column(String(300))
    at: Mapped[datetime] = _now()


class WaTemplate(Base):
    """``tenant_id`` NULL = platform-wide (readable by every tenant, writable by none)."""

    __tablename__ = "wa_template"

    id: Mapped[uuid.UUID] = _pk_uuid()
    tenant_id: Mapped[uuid.UUID | None] = _opt_uuid()
    code: Mapped[str] = mapped_column(String(40), nullable=False)
    provider_template_name: Mapped[str] = mapped_column(String(512), nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    language: Mapped[str] = mapped_column(String(10), nullable=False, server_default="en")
    variables: Mapped[Any] = mapped_column(JSONB, nullable=False, server_default="[]")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="PENDING")
    updated_at: Mapped[datetime] = _now()


class Message(Base):
    __tablename__ = "message"

    id: Mapped[uuid.UUID] = _pk_uuid()
    tenant_id: Mapped[uuid.UUID] = _uuid_col()
    outlet_id: Mapped[uuid.UUID | None] = _opt_uuid()
    customer_id: Mapped[uuid.UUID | None] = _opt_uuid()
    phone: Mapped[str] = mapped_column(String(16), nullable=False)
    purpose: Mapped[str] = mapped_column(String(8), nullable=False)
    template_code: Mapped[str] = mapped_column(String(40), nullable=False)
    language: Mapped[str] = mapped_column(String(10), nullable=False, server_default="en")
    params: Mapped[Any] = mapped_column(JSONB, nullable=False, server_default="[]")
    buttons: Mapped[Any] = mapped_column(JSONB, nullable=False, server_default="[]")
    ref_type: Mapped[str | None] = mapped_column(String(40))
    ref_id: Mapped[str] = mapped_column(String(120), nullable=False)
    campaign_id: Mapped[uuid.UUID | None] = _opt_uuid()
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default="QUEUED")
    skip_reason: Mapped[str | None] = mapped_column(String(40))
    provider: Mapped[str | None] = mapped_column(String(20))
    provider_msg_id: Mapped[str | None] = mapped_column(String(200))
    error: Mapped[str | None] = mapped_column(String(500))
    category: Mapped[str | None] = mapped_column(String(16))
    attempts: Mapped[int] = _count()
    created_at: Mapped[datetime] = _now()
    sent_at: Mapped[datetime | None] = mapped_column(_TS)
    delivered_at: Mapped[datetime | None] = mapped_column(_TS)
    read_at: Mapped[datetime | None] = mapped_column(_TS)
    failed_at: Mapped[datetime | None] = mapped_column(_TS)


class Campaign(Base):
    __tablename__ = "campaign"

    id: Mapped[uuid.UUID] = _pk_uuid()
    tenant_id: Mapped[uuid.UUID] = _uuid_col()
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    template_code: Mapped[str] = mapped_column(String(40), nullable=False)
    language: Mapped[str] = mapped_column(String(10), nullable=False, server_default="en")
    params: Mapped[Any] = mapped_column(JSONB, nullable=False, server_default="[]")
    buttons: Mapped[Any] = mapped_column(JSONB, nullable=False, server_default="[]")
    outlet_ids: Mapped[Any | None] = mapped_column(JSONB)
    visited_within_days: Mapped[int | None] = mapped_column(Integer)
    scheduled_at: Mapped[datetime | None] = mapped_column(_TS)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="DRAFT")
    recipients: Mapped[int] = _count()
    sent: Mapped[int] = _count()
    failed: Mapped[int] = _count()
    skipped: Mapped[int] = _count()
    est_cost_paise: Mapped[int | None] = mapped_column(Integer)
    created_by: Mapped[uuid.UUID | None] = _opt_uuid()
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()


class TenantNotifySettings(Base):
    __tablename__ = "tenant_notify_settings"

    tenant_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    promo_cap_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default="7")
    updated_at: Mapped[datetime] = _now()


# ---- Cross-tenant index tables (no RLS; ids only) ----


class MessageRef(Base):
    __tablename__ = "message_ref"

    provider: Mapped[str] = mapped_column(String(20), primary_key=True)
    provider_msg_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    message_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)


class CustomerPhoneRef(Base):
    """sha256(E.164 phone) -> customer, per tenant. Maintained by a trigger on ``customer``."""

    __tablename__ = "customer_phone_ref"

    phone_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)


class InboundEvent(Base):
    __tablename__ = "inbound_event"

    provider: Mapped[str] = mapped_column(String(20), primary_key=True)
    provider_msg_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    received_at: Mapped[datetime] = _now()


class CampaignDue(Base):
    __tablename__ = "campaign_due"

    campaign_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(_TS, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(_TS)
