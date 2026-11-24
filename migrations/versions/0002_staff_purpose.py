"""STAFF purpose (employee reminders from inventory-service) + its platform templates

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-24

* ``message.purpose`` accepts ``STAFF`` (utility; no marketing-consent check, like BILL).
* ``wa_template.code`` accepts the platform codes ``STAFF_COUNT_DUE``, ``STAFF_HANDOVER`` and
  ``STAFF_DEDUCTION``. They are seeded platform-wide (``tenant_id`` NULL, UTILITY, PENDING) like
  ``sg_bill`` / ``sg_otp``; ops flip the status once Meta approves them. Tenants still can't
  write platform rows (RLS) and the admin API only accepts ``PROMO_*`` codes.

Params (positional): ``[restaurant, kit name]`` / ``[restaurant, kit name]`` /
``[restaurant, item, amount]``.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CODES_V2 = "^(BILL|OTP|STAFF_COUNT_DUE|STAFF_HANDOVER|STAFF_DEDUCTION|PROMO_[A-Z0-9_]{1,30})$"
_CODES_V1 = "^(BILL|OTP|PROMO_[A-Z0-9_]{1,30})$"


def upgrade() -> None:
    op.execute(
        f"""
        ALTER TABLE message DROP CONSTRAINT IF EXISTS message_purpose_check;
        ALTER TABLE message ADD CONSTRAINT message_purpose_check
            CHECK (purpose IN ('BILL', 'PROMO', 'OTP', 'STAFF'));

        ALTER TABLE wa_template DROP CONSTRAINT IF EXISTS wa_template_code_check;
        ALTER TABLE wa_template ADD CONSTRAINT wa_template_code_check
            CHECK (code ~ '{_CODES_V2}');

        -- FORCE RLS applies to the table owner too (on Aurora the migrating admin is the owner,
        -- not a superuser), and platform rows (tenant_id NULL) fail every tenant policy; lift it
        -- for the seed only, inside this migration's transaction.
        ALTER TABLE wa_template NO FORCE ROW LEVEL SECURITY;
        INSERT INTO wa_template (tenant_id, code, provider_template_name, category, language,
                                 variables, status)
        VALUES
            (NULL, 'STAFF_COUNT_DUE', 'sg_staff_count_due', 'UTILITY', 'en',
             '["restaurant", "kit"]'::jsonb, 'PENDING'),
            (NULL, 'STAFF_HANDOVER', 'sg_staff_handover', 'UTILITY', 'en',
             '["restaurant", "kit"]'::jsonb, 'PENDING'),
            (NULL, 'STAFF_DEDUCTION', 'sg_staff_deduction', 'UTILITY', 'en',
             '["restaurant", "item", "amount"]'::jsonb, 'PENDING')
        ON CONFLICT DO NOTHING;
        ALTER TABLE wa_template FORCE ROW LEVEL SECURITY;
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        ALTER TABLE wa_template NO FORCE ROW LEVEL SECURITY;
        ALTER TABLE message NO FORCE ROW LEVEL SECURITY;
        DELETE FROM wa_template WHERE tenant_id IS NULL AND code LIKE 'STAFF\\_%';
        DELETE FROM message WHERE purpose = 'STAFF';
        ALTER TABLE wa_template FORCE ROW LEVEL SECURITY;
        ALTER TABLE message FORCE ROW LEVEL SECURITY;
        ALTER TABLE wa_template DROP CONSTRAINT IF EXISTS wa_template_code_check;
        ALTER TABLE wa_template ADD CONSTRAINT wa_template_code_check
            CHECK (code ~ '{_CODES_V1}');
        ALTER TABLE message DROP CONSTRAINT IF EXISTS message_purpose_check;
        ALTER TABLE message ADD CONSTRAINT message_purpose_check
            CHECK (purpose IN ('BILL', 'PROMO', 'OTP'));
        """
    )
