"""notify baseline: customers, append-only consent, WhatsApp templates, messages, campaigns,
per-tenant settings (+ Row-Level Security) and the narrow cross-tenant lookup tables

Revision ID: 0001
Revises:
Create Date: 2026-11-15

Tenant data (``customer``, ``customer_consent``, ``wa_template``, ``message``, ``campaign``,
``tenant_notify_settings``) is under RLS on ``tenant_id`` like every other service.
``wa_template`` rows with ``tenant_id IS NULL`` are platform-wide (the shared WhatsApp number's
BILL / OTP templates): every tenant can read them, no tenant can write them.

Three paths have no tenant context and must cross tenants: the provider webhook (a status update
carries only the provider message id; a STOP reply carries only the phone) and the campaign
scheduler (which campaigns are due). Rather than a SECURITY DEFINER function (on Aurora the owner
is not a superuser, and FORCE RLS applies to it too), they use small **non-RLS index tables that
hold ids only**, never message content:

* ``message_ref``          provider_msg_id -> (tenant_id, message_id)
* ``customer_phone_ref``   sha256(phone) -> (tenant_id, customer_id); maintained by a trigger on
  ``customer`` so it cannot drift, and hashed so no phone number sits outside RLS
* ``inbound_event``        provider inbound message ids already handled (webhook retry dedup)
* ``campaign_due``         campaign_id -> (tenant_id, scheduled_at, lease)

Every write to the tenant tables after such a lookup still happens in a tenant-scoped session.
``customer_consent`` is append-only (trigger + grants), like order-service's ``pos_ticket_event``.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RLS_TABLES = (
    "customer",
    "customer_consent",
    "message",
    "campaign",
    "tenant_notify_settings",
)
_INDEX_TABLES = ("message_ref", "customer_phone_ref", "inbound_event", "campaign_due")
_CURRENT = "nullif(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE customer (
            id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id            uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
            phone                varchar(16) NOT NULL CHECK (phone ~ '^\\+[1-9][0-9]{7,14}$'),
            name                 varchar(120),
            first_seen_outlet_id uuid,
            last_outlet_id       uuid,
            last_visit_at        timestamptz,
            visit_count          integer NOT NULL DEFAULT 0,
            created_at           timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_customer_phone UNIQUE (tenant_id, phone)
        );
        CREATE INDEX ix_customer_tenant_created ON customer (tenant_id, created_at, id);

        CREATE TABLE customer_consent (
            id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            customer_id uuid NOT NULL REFERENCES customer(id) ON DELETE CASCADE,
            tenant_id   uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
            channel     varchar(16) NOT NULL DEFAULT 'WHATSAPP' CHECK (channel IN ('WHATSAPP')),
            purpose     varchar(16) NOT NULL DEFAULT 'MARKETING' CHECK (purpose IN ('MARKETING')),
            status      varchar(16) NOT NULL CHECK (status IN ('OPTED_IN', 'OPTED_OUT')),
            source      varchar(16) NOT NULL
                        CHECK (source IN ('POS', 'STOP_REPLY', 'ADMIN', 'IMPORT')),
            outlet_id   uuid,
            actor_id    uuid,
            note        varchar(300),
            at          timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_customer_consent_latest ON customer_consent (customer_id, id DESC);
        CREATE INDEX ix_customer_consent_tenant_id ON customer_consent (tenant_id);

        -- Append-only: no UPDATE / DELETE whoever the caller is, except the FK cascade when the
        -- tenant or customer itself is deleted (the parent row is already gone then).
        CREATE FUNCTION customer_consent_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' AND (
                NOT EXISTS (SELECT 1 FROM tenant WHERE id = OLD.tenant_id)
                OR NOT EXISTS (SELECT 1 FROM customer WHERE id = OLD.customer_id)
            ) THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'customer_consent is append-only';
        END $$;
        CREATE TRIGGER trg_customer_consent_append_only
            BEFORE UPDATE OR DELETE ON customer_consent
            FOR EACH ROW EXECUTE FUNCTION customer_consent_append_only();

        CREATE TABLE wa_template (
            id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id              uuid REFERENCES tenant(id) ON DELETE CASCADE,
            code                   varchar(40) NOT NULL
                                   CHECK (code ~ '^(BILL|OTP|PROMO_[A-Z0-9_]{1,30})$'),
            provider_template_name varchar(512) NOT NULL,
            category               varchar(16) NOT NULL
                                   CHECK (category IN ('MARKETING', 'UTILITY', 'AUTHENTICATION')),
            language               varchar(10) NOT NULL DEFAULT 'en',
            variables              jsonb NOT NULL DEFAULT '[]'::jsonb,
            status                 varchar(16) NOT NULL DEFAULT 'PENDING'
                                   CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED')),
            updated_at             timestamptz NOT NULL DEFAULT now()
        );
        -- (tenant, code, language) unique, with NULL (platform) treated as one value
        CREATE UNIQUE INDEX uq_wa_template ON wa_template
            (coalesce(tenant_id, '00000000-0000-0000-0000-000000000000'::uuid), code, language);

        CREATE TABLE campaign (
            id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id           uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
            name                varchar(120) NOT NULL,
            template_code       varchar(40) NOT NULL,
            language            varchar(10) NOT NULL DEFAULT 'en',
            params              jsonb NOT NULL DEFAULT '[]'::jsonb,
            buttons             jsonb NOT NULL DEFAULT '[]'::jsonb,
            outlet_ids          jsonb,
            visited_within_days integer CHECK (visited_within_days > 0),
            scheduled_at        timestamptz,
            status              varchar(16) NOT NULL DEFAULT 'DRAFT'
                                CHECK (status IN ('DRAFT', 'SCHEDULED', 'SENDING', 'DONE',
                                                  'CANCELLED')),
            recipients          integer NOT NULL DEFAULT 0,
            sent                integer NOT NULL DEFAULT 0,
            failed              integer NOT NULL DEFAULT 0,
            skipped             integer NOT NULL DEFAULT 0,
            est_cost_paise      integer,
            created_by          uuid,
            created_at          timestamptz NOT NULL DEFAULT now(),
            updated_at          timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_campaign_tenant_created ON campaign (tenant_id, created_at DESC);

        CREATE TABLE message (
            id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id       uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
            outlet_id       uuid,
            customer_id     uuid REFERENCES customer(id) ON DELETE SET NULL,
            phone           varchar(16) NOT NULL,
            purpose         varchar(8) NOT NULL CHECK (purpose IN ('BILL', 'PROMO', 'OTP')),
            template_code   varchar(40) NOT NULL,
            language        varchar(10) NOT NULL DEFAULT 'en',
            params          jsonb NOT NULL DEFAULT '[]'::jsonb,
            buttons         jsonb NOT NULL DEFAULT '[]'::jsonb,
            ref_type        varchar(40),
            ref_id          varchar(120) NOT NULL,
            campaign_id     uuid REFERENCES campaign(id) ON DELETE SET NULL,
            status          varchar(10) NOT NULL DEFAULT 'QUEUED'
                            CHECK (status IN ('QUEUED', 'SENT', 'DELIVERED', 'READ', 'FAILED',
                                              'SKIPPED')),
            skip_reason     varchar(40),
            provider        varchar(20),
            provider_msg_id varchar(200),
            error           varchar(500),
            category        varchar(16),
            attempts        integer NOT NULL DEFAULT 0,
            created_at      timestamptz NOT NULL DEFAULT now(),
            sent_at         timestamptz,
            delivered_at    timestamptz,
            read_at         timestamptz,
            failed_at       timestamptz,
            -- idempotency: a retried / duplicated SQS message never sends twice
            CONSTRAINT uq_message_idem UNIQUE (tenant_id, purpose, ref_id, phone)
        );
        CREATE INDEX ix_message_tenant_created ON message (tenant_id, created_at DESC, id DESC);
        CREATE INDEX ix_message_promo_cap ON message (tenant_id, phone, purpose, sent_at);
        CREATE INDEX ix_message_campaign ON message (campaign_id);

        CREATE TABLE tenant_notify_settings (
            tenant_id      uuid PRIMARY KEY REFERENCES tenant(id) ON DELETE CASCADE,
            promo_cap_days integer NOT NULL DEFAULT 7 CHECK (promo_cap_days BETWEEN 0 AND 365),
            updated_at     timestamptz NOT NULL DEFAULT now()
        );

        -- ---- Non-RLS cross-tenant index tables (ids only; see module docstring) ----
        CREATE TABLE message_ref (
            provider        varchar(20) NOT NULL,
            provider_msg_id varchar(200) NOT NULL,
            tenant_id       uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
            message_id      uuid NOT NULL REFERENCES message(id) ON DELETE CASCADE,
            PRIMARY KEY (provider, provider_msg_id)
        );

        CREATE TABLE customer_phone_ref (
            phone_hash  char(64) NOT NULL,
            tenant_id   uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
            customer_id uuid NOT NULL REFERENCES customer(id) ON DELETE CASCADE,
            PRIMARY KEY (phone_hash, tenant_id)
        );

        CREATE FUNCTION customer_phone_ref_sync() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'UPDATE' AND NEW.phone = OLD.phone THEN
                RETURN NEW;
            END IF;
            IF TG_OP = 'UPDATE' THEN
                DELETE FROM customer_phone_ref WHERE customer_id = OLD.id;
            END IF;
            INSERT INTO customer_phone_ref (phone_hash, tenant_id, customer_id)
            VALUES (encode(sha256(convert_to(NEW.phone, 'UTF8')), 'hex'), NEW.tenant_id, NEW.id)
            ON CONFLICT DO NOTHING;
            RETURN NEW;
        END $$;
        CREATE TRIGGER trg_customer_phone_ref
            AFTER INSERT OR UPDATE OF phone ON customer
            FOR EACH ROW EXECUTE FUNCTION customer_phone_ref_sync();

        CREATE TABLE inbound_event (
            provider        varchar(20) NOT NULL,
            provider_msg_id varchar(200) NOT NULL,
            received_at     timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (provider, provider_msg_id)
        );

        CREATE TABLE campaign_due (
            campaign_id  uuid PRIMARY KEY REFERENCES campaign(id) ON DELETE CASCADE,
            tenant_id    uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
            scheduled_at timestamptz NOT NULL,
            locked_until timestamptz
        );
        CREATE INDEX ix_campaign_due_at ON campaign_due (scheduled_at);

        -- Platform-wide templates for the shared number. PENDING until Meta approves them; ops
        -- then flip status (and the real template names) with SQL as the admin user.
        INSERT INTO wa_template (tenant_id, code, provider_template_name, category, language,
                                 variables, status)
        VALUES
            (NULL, 'BILL', 'sg_bill', 'UTILITY', 'en',
             '["store", "bill_no", "total", "url_suffix"]'::jsonb, 'PENDING'),
            (NULL, 'OTP', 'sg_otp', 'AUTHENTICATION', 'en', '["code"]'::jsonb, 'PENDING');
        """
    )

    for table in _RLS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
                USING (tenant_id = {_CURRENT})
                WITH CHECK (tenant_id = {_CURRENT});
            """
        )

    # Templates: platform rows (tenant_id NULL) readable by all tenants, writable by none.
    op.execute("ALTER TABLE wa_template ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE wa_template FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"""
        CREATE POLICY template_read ON wa_template FOR SELECT
            USING (tenant_id IS NULL OR tenant_id = {_CURRENT});
        CREATE POLICY template_write ON wa_template FOR ALL
            USING (tenant_id = {_CURRENT})
            WITH CHECK (tenant_id = {_CURRENT});
        """
    )


def downgrade() -> None:
    for table in _INDEX_TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE;")
    for table in ("wa_template", *reversed(_RLS_TABLES)):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE;")
    op.execute("DROP FUNCTION IF EXISTS customer_consent_append_only();")
    op.execute("DROP FUNCTION IF EXISTS customer_phone_ref_sync();")
