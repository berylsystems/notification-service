"""Schema-level guarantees: the chain applies with its own version table, RLS isolates tenants
(and lets every tenant read platform templates), consent is append-only, and the phone index is
maintained by trigger with a hash (no phone number outside RLS)."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest
from sg_commons.db import tenant_session
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from helpers import admin_fetch, new_tenant, phone
from notification_service.domain.enums import ConsentStatus
from notification_service.services import customers


def test_migrations_applied(admin_url: str, app_url: str) -> None:
    rows = admin_fetch(admin_url, "SELECT version_num FROM alembic_version_notify")
    assert [r[0] for r in rows] == ["0002"]
    tables = {
        r[0]
        for r in admin_fetch(
            admin_url, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )
    }
    assert {
        "customer",
        "customer_consent",
        "wa_template",
        "message",
        "campaign",
        "tenant_notify_settings",
        "message_ref",
        "customer_phone_ref",
        "inbound_event",
        "campaign_due",
    } <= tables
    rls = {
        r[0]
        for r in admin_fetch(
            admin_url, "SELECT relname FROM pg_class WHERE relrowsecurity AND relforcerowsecurity"
        )
    }
    assert {
        "customer",
        "customer_consent",
        "wa_template",
        "message",
        "campaign",
        "tenant_notify_settings",
    } <= rls


def test_rls_isolation(admin_url: str, engine: Any) -> None:
    a, b = new_tenant(admin_url), new_tenant(admin_url)
    pa = phone()
    with tenant_session(engine, a) as s:
        customers.upsert_customer(s, a, pa, name="A")
    with tenant_session(engine, b) as s:
        assert customers.find_customer_id(s, pa) is None
        assert s.execute(text("SELECT count(*) FROM customer")).scalar_one() == 0
        with pytest.raises(ProgrammingError):  # WITH CHECK: can't write another tenant's row
            s.execute(
                text("INSERT INTO customer (tenant_id, phone) VALUES (:t, :p)"),
                {"t": a, "p": phone()},
            )


def test_platform_templates_readable_not_writable(admin_url: str, engine: Any) -> None:
    t = new_tenant(admin_url)
    with tenant_session(engine, t) as s:
        codes = {r[0] for r in s.execute(text("SELECT code FROM wa_template")).all()}
        assert {"BILL", "OTP", "STAFF_COUNT_DUE", "STAFF_HANDOVER", "STAFF_DEDUCTION"} <= codes
        updated = s.execute(
            text("UPDATE wa_template SET status = 'REJECTED' WHERE tenant_id IS NULL")
        ).rowcount  # type: ignore[attr-defined]
        assert updated == 0


def test_consent_append_only(admin_url: str, engine: Any) -> None:
    t = new_tenant(admin_url)
    with tenant_session(engine, t) as s:
        cid = customers.upsert_customer(s, t, phone())
        customers.append_consent(s, t, cid, ConsentStatus.OPTED_IN, "POS")
    for stmt in (
        "UPDATE customer_consent SET status = 'OPTED_OUT'",
        "DELETE FROM customer_consent",
    ):
        with pytest.raises(DBAPIError), tenant_session(engine, t) as s:
            s.execute(text(stmt))
    # even the owner/superuser is blocked by the trigger
    from sg_commons.db import make_engine

    admin = make_engine(admin_url)
    with pytest.raises(DBAPIError), admin.begin() as conn:
        conn.execute(text("UPDATE customer_consent SET note = 'x' WHERE tenant_id = :t"), {"t": t})
    # ...but deleting the customer cascades
    with admin.begin() as conn:
        conn.execute(text("DELETE FROM customer WHERE tenant_id = :t"), {"t": t})
    assert (
        admin_fetch(admin_url, "SELECT count(*) FROM customer_consent WHERE tenant_id = :t", t=t)[
            0
        ][0]
        == 0
    )
    admin.dispose()


def test_only_if_changed(admin_url: str, engine: Any) -> None:
    t = new_tenant(admin_url)
    with tenant_session(engine, t) as s:
        cid = customers.upsert_customer(s, t, phone())
        assert customers.append_consent(s, t, cid, ConsentStatus.OPTED_IN, "POS")
        assert not customers.append_consent(s, t, cid, ConsentStatus.OPTED_IN, "POS")
        assert customers.append_consent(s, t, cid, ConsentStatus.OPTED_OUT, "ADMIN")
        assert customers.latest_consent(s, cid) == "OPTED_OUT"


def test_phone_ref_trigger_hashes(admin_url: str, engine: Any) -> None:
    a, b = new_tenant(admin_url), new_tenant(admin_url)
    p = phone()
    with tenant_session(engine, a) as s:
        ca = customers.upsert_customer(s, a, p)
    with tenant_session(engine, b) as s:
        cb = customers.upsert_customer(s, b, p)
    rows = admin_fetch(
        admin_url,
        "SELECT phone_hash, tenant_id, customer_id FROM customer_phone_ref "
        "WHERE customer_id IN (:a, :b)",
        a=ca,
        b=cb,
    )
    assert {r[0] for r in rows} == {hashlib.sha256(p.encode()).hexdigest()}
    assert {(r[1], r[2]) for r in rows} == {(a, ca), (b, cb)}
