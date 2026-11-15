"""Processing one ``sg-notify-send`` queue message (see ``domain/contract.py``).

SEND, in two transactions (both tenant-scoped):

1. **Record + decide.** Insert the ``message`` row (status QUEUED; OTP params / button values
   stored as ``******``) with ``ON CONFLICT DO NOTHING`` on the idempotency key
   ``(tenant_id, purpose, ref_id, phone)``. A duplicate whose row is already past QUEUED is
   dropped. A new BILL row counts a visit on the customer. Then the rules: the template must exist
   (tenant row overrides platform row) and be APPROVED, else FAILED; a PROMO needs the customer's
   latest consent OPTED_IN and no PROMO sent within the tenant's frequency cap (default 7 days),
   and a cancelled campaign stops it, else SKIPPED with a reason.
2. **Send.** Re-select the row ``FOR UPDATE`` while still QUEUED (so two concurrent deliveries of
   the same SQS message can't both send), call the provider with the *unredacted* params from the
   queue message, then record SENT + ``provider_msg_id`` (and the cross-tenant ``message_ref``),
   or FAILED on a permanent error. A transient error is recorded (attempts, error) and re-raised
   so SQS redelivers (DLQ after 5 receives).

Campaign counters (``sent`` / ``failed`` / ``skipped``) move exactly once per message: only on its
first transition out of QUEUED.

CONSENT: upsert the customer and append the consent change if it differs from the current state.

Logs never contain a full phone number or an OTP: phones are masked (``+91******3210``).
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from aws_lambda_powertools import Logger
from sg_commons.db import tenant_session
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from ..domain.contract import ConsentMessage, SendMessage, parse_message
from ..domain.enums import (
    ConsentStatus,
    MessageStatus,
    Purpose,
    SkipReason,
    TemplateStatus,
    mask_phone,
    redact_buttons,
    redact_params,
)
from ..providers.base import (
    Button,
    PermanentProviderError,
    TransientProviderError,
    WhatsAppProvider,
)
from . import customers
from .common import utcnow

logger = Logger(service="notification-service", child=True)

_COUNTER = {
    MessageStatus.SENT: "sent",
    MessageStatus.FAILED: "failed",
    MessageStatus.SKIPPED: "skipped",
}


def handle(engine: Engine, provider: WhatsAppProvider, body: dict[str, Any]) -> str:
    """Process one queue message body; returns the outcome (message status or ``CONSENT``)."""
    msg = parse_message(body)
    if isinstance(msg, ConsentMessage):
        apply_consent(engine, msg)
        return "CONSENT"
    return send(engine, provider, msg).value


# =============================================================================================
# CONSENT
# =============================================================================================


def apply_consent(engine: Engine, msg: ConsentMessage) -> bool:
    with tenant_session(engine, msg.tenant_id) as s:
        customer_id = customers.upsert_customer(
            s, msg.tenant_id, msg.phone, name=msg.name, outlet_id=msg.outlet_id
        )
        appended = customers.append_consent(
            s,
            msg.tenant_id,
            customer_id,
            ConsentStatus(msg.status),
            msg.source.value,
            outlet_id=msg.outlet_id,
            actor_id=msg.actor_id,
            note=msg.note,
        )
    logger.info(
        "consent", extra={"phone": mask_phone(msg.phone), "status": msg.status.value,
                          "appended": appended}
    )  # fmt: skip
    return appended


# =============================================================================================
# SEND
# =============================================================================================


def _bump_campaign(s: Session, campaign_id: UUID | None, status: MessageStatus) -> None:
    column = _COUNTER.get(status)
    if campaign_id is None or column is None:
        return
    s.execute(
        text(f"UPDATE campaign SET {column} = {column} + 1, updated_at = now() WHERE id = :id"),
        {"id": campaign_id},
    )


def _finish(
    s: Session,
    message_id: UUID,
    campaign_id: UUID | None,
    status: MessageStatus,
    *,
    reason: str | None = None,
    error: str | None = None,
) -> MessageStatus:
    """QUEUED -> SKIPPED / FAILED during the decision step."""
    s.execute(
        text(
            """
            UPDATE message SET status = :st, skip_reason = :reason, error = :error,
                   failed_at = CASE WHEN :failed THEN now() ELSE failed_at END
            WHERE id = :id AND status = 'QUEUED'
            """
        ),
        {
            "st": status.value,
            "reason": reason,
            "error": error,
            "id": message_id,
            "failed": status is MessageStatus.FAILED,
        },
    )
    _bump_campaign(s, campaign_id, status)
    return status


def _template(s: Session, code: str, language: str) -> dict[str, Any] | None:
    row = (
        s.execute(
            text(
                """
                SELECT provider_template_name, category, status FROM wa_template
                WHERE code = :code AND language = :lang
                ORDER BY tenant_id NULLS LAST LIMIT 1
                """
            ),
            {"code": code, "lang": language},
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def promo_cap_days(s: Session) -> int:
    row = s.execute(text("SELECT promo_cap_days FROM tenant_notify_settings")).first()
    return int(row[0]) if row else 7


def _decide(s: Session, msg: SendMessage, message_id: UUID, customer_id: UUID | None) -> str | None:
    """Apply the rules; returns the template's provider name when the message may be sent."""
    template = _template(s, msg.template_code, msg.language)
    if template is None:
        _finish(s, message_id, msg.campaign_id, MessageStatus.FAILED,
                reason=SkipReason.TEMPLATE_NOT_FOUND, error="template not found")  # fmt: skip
        return None
    s.execute(
        text("UPDATE message SET category = :c WHERE id = :id"),
        {"c": template["category"], "id": message_id},
    )
    if template["status"] != TemplateStatus.APPROVED:
        _finish(s, message_id, msg.campaign_id, MessageStatus.FAILED,
                reason=SkipReason.TEMPLATE_NOT_APPROVED,
                error=f"template {template['status'].lower()}")  # fmt: skip
        return None

    if msg.purpose is Purpose.PROMO:
        if msg.campaign_id is not None:
            row = s.execute(
                text("SELECT status FROM campaign WHERE id = :id"), {"id": msg.campaign_id}
            ).first()
            if row is not None and row[0] == "CANCELLED":
                _finish(s, message_id, msg.campaign_id, MessageStatus.SKIPPED,
                        reason=SkipReason.CAMPAIGN_CANCELLED)  # fmt: skip
                return None
        consent = customers.latest_consent(s, customer_id) if customer_id else None
        if consent != ConsentStatus.OPTED_IN:
            _finish(s, message_id, msg.campaign_id, MessageStatus.SKIPPED,
                    reason=SkipReason.NO_CONSENT)  # fmt: skip
            return None
        cap = promo_cap_days(s)
        if cap > 0:
            recent = s.execute(
                text(
                    """
                    SELECT 1 FROM message
                    WHERE phone = :phone AND purpose = 'PROMO' AND id <> :id
                      AND status IN ('SENT', 'DELIVERED', 'READ')
                      AND sent_at > now() - make_interval(days => :cap)
                    LIMIT 1
                    """
                ),
                {"phone": msg.to, "id": message_id, "cap": cap},
            ).first()
            if recent is not None:
                _finish(s, message_id, msg.campaign_id, MessageStatus.SKIPPED,
                        reason=SkipReason.FREQUENCY_CAP)  # fmt: skip
                return None
    return str(template["provider_template_name"])


def _record(s: Session, msg: SendMessage) -> tuple[UUID, str, UUID | None]:
    """Insert (or find) the message row. Returns (id, status, customer_id)."""
    is_otp = msg.purpose is Purpose.OTP
    params: list[str] | dict[str, str] = redact_params(msg.params) if is_otp else msg.params
    buttons = [b.model_dump() for b in msg.buttons]
    stored_buttons = redact_buttons(buttons) if is_otp else buttons
    row = s.execute(
        text(
            """
            INSERT INTO message (tenant_id, outlet_id, phone, purpose, template_code, language,
                                 params, buttons, ref_type, ref_id, campaign_id)
            VALUES (:tid, :outlet, :phone, :purpose, :code, :lang, CAST(:params AS jsonb),
                    CAST(:buttons AS jsonb), :ref_type, :ref_id, :campaign)
            ON CONFLICT (tenant_id, purpose, ref_id, phone) DO NOTHING
            RETURNING id
            """
        ),
        {
            "tid": msg.tenant_id,
            "outlet": msg.outlet_id,
            "phone": msg.to,
            "purpose": msg.purpose.value,
            "code": msg.template_code,
            "lang": msg.language,
            "params": json.dumps(params),
            "buttons": json.dumps(stored_buttons),
            "ref_type": msg.ref_type,
            "ref_id": msg.ref_id,
            "campaign": msg.campaign_id,
        },
    ).first()
    if row is None:  # duplicate delivery / retry
        existing = s.execute(
            text(
                """
                SELECT id, status, customer_id FROM message
                WHERE purpose = :purpose AND ref_id = :ref_id AND phone = :phone
                """
            ),
            {"purpose": msg.purpose.value, "ref_id": msg.ref_id, "phone": msg.to},
        ).one()
        return existing[0], str(existing[1]), existing[2]

    message_id: UUID = row[0]
    if msg.purpose is Purpose.BILL:
        customer_id: UUID | None = customers.upsert_customer(
            s, msg.tenant_id, msg.to, name=msg.customer_name, outlet_id=msg.outlet_id,
            visit_at=utcnow(),
        )  # fmt: skip
    else:
        customer_id = customers.find_customer_id(s, msg.to)
    if customer_id is not None:
        s.execute(
            text("UPDATE message SET customer_id = :c WHERE id = :id"),
            {"c": customer_id, "id": message_id},
        )
    return message_id, MessageStatus.QUEUED.value, customer_id


def send(engine: Engine, provider: WhatsAppProvider, msg: SendMessage) -> MessageStatus:
    log = {"purpose": msg.purpose.value, "phone": mask_phone(msg.to), "ref_type": msg.ref_type}

    # ---- 1. record + decide ----
    with tenant_session(engine, msg.tenant_id) as s:
        message_id, status, customer_id = _record(s, msg)
        if status != MessageStatus.QUEUED:
            logger.info("duplicate; already handled", extra={**log, "status": status})
            return MessageStatus(status)
        template_name = _decide(s, msg, message_id, customer_id)
    if template_name is None:
        logger.info("not sent", extra={**log, "message_id": str(message_id)})
        with tenant_session(engine, msg.tenant_id) as s:
            row = s.execute(
                text("SELECT status FROM message WHERE id = :id"), {"id": message_id}
            ).one()
        return MessageStatus(row[0])

    # ---- 2. send (row locked while QUEUED) ----
    transient: TransientProviderError | None = None
    outcome = MessageStatus.QUEUED
    with tenant_session(engine, msg.tenant_id) as s:
        locked = s.execute(
            text("SELECT status FROM message WHERE id = :id AND status = 'QUEUED' FOR UPDATE"),
            {"id": message_id},
        ).first()
        if locked is None:  # a concurrent delivery got there first
            row = s.execute(
                text("SELECT status FROM message WHERE id = :id"), {"id": message_id}
            ).one()
            return MessageStatus(row[0])
        s.execute(
            text("UPDATE message SET attempts = attempts + 1, provider = :p WHERE id = :id"),
            {"p": provider.name, "id": message_id},
        )
        try:
            provider_msg_id = provider.send_template(
                msg.to,
                template_name,
                msg.language,
                msg.params,
                [Button(b.type, b.value) for b in msg.buttons],
            )
        except TransientProviderError as exc:
            transient = exc
            s.execute(
                text("UPDATE message SET error = :e WHERE id = :id"),
                {"e": str(exc)[:500], "id": message_id},
            )
        except PermanentProviderError as exc:
            s.execute(
                text(
                    """
                    UPDATE message SET status = 'FAILED', error = :e, failed_at = now()
                    WHERE id = :id
                    """
                ),
                {"e": str(exc)[:500], "id": message_id},
            )
            _bump_campaign(s, msg.campaign_id, MessageStatus.FAILED)
            outcome = MessageStatus.FAILED
        else:
            s.execute(
                text(
                    """
                    UPDATE message SET status = 'SENT', provider_msg_id = :pid, sent_at = now(),
                           error = NULL
                    WHERE id = :id
                    """
                ),
                {"pid": provider_msg_id, "id": message_id},
            )
            s.execute(
                text(
                    """
                    INSERT INTO message_ref (provider, provider_msg_id, tenant_id, message_id)
                    VALUES (:p, :pid, :tid, :id) ON CONFLICT DO NOTHING
                    """
                ),
                {"p": provider.name, "pid": provider_msg_id, "tid": msg.tenant_id,
                 "id": message_id},
            )  # fmt: skip
            _bump_campaign(s, msg.campaign_id, MessageStatus.SENT)
            outcome = MessageStatus.SENT

    if transient is not None:
        logger.warning(
            "transient provider error; SQS will retry",
            extra={**log, "message_id": str(message_id), "code": transient.code},
        )
        raise transient
    logger.info("sent" if outcome is MessageStatus.SENT else "failed",
                extra={**log, "message_id": str(message_id)})  # fmt: skip
    return outcome
