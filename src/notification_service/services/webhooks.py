"""Provider webhook processing (the HTTP route verifies the signature first).

* **Status updates** carry only the provider message id: ``message_ref`` (non-RLS, ids only)
  maps it to the tenant, then the row is updated in a tenant-scoped session, forward-only
  (QUEUED < SENT < DELIVERED < READ; FAILED only from QUEUED/SENT), so out-of-order webhooks never
  regress READ to DELIVERED. A READ also fills a missing ``delivered_at``.
* **Inbound messages** are de-duplicated by provider id (``inbound_event``; providers retry
  webhooks). A STOP-type reply (``STOP``, ``STOP ALL``, ``UNSUBSCRIBE``, ``stop promotions``,
  case-insensitive) appends an OPTED_OUT consent (source STOP_REPLY) in **every** tenant where the
  phone is a customer -- the number is shared, so the guest can't tell restaurants apart -- and
  sends one confirmation text (free inside the 24-hour window). START and anything else are
  ignored: opting in happens only at the POS or by an admin.
"""

from __future__ import annotations

from typing import Any

from aws_lambda_powertools import Logger
from sg_commons.db import admin_session, tenant_session
from sqlalchemy import Engine, text

from ..domain.enums import (
    STOP_WORDS,
    ConsentSource,
    ConsentStatus,
    MessageStatus,
    mask_phone,
    status_advances,
)
from ..providers.base import InboundMessage, ProviderError, StatusUpdate, WhatsAppProvider
from . import customers

logger = Logger(service="notification-service", child=True)

STOP_CONFIRMATION = (
    "You have been unsubscribed from promotional WhatsApp messages from restaurants using this "
    "number. You will still receive bills and sign-in codes you ask for."
)
_TS_COLUMN = {
    MessageStatus.SENT: "sent_at",
    MessageStatus.DELIVERED: "delivered_at",
    MessageStatus.READ: "read_at",
    MessageStatus.FAILED: "failed_at",
}


def is_stop(text_body: str) -> bool:
    return " ".join(text_body.split()).casefold() in STOP_WORDS


def apply_status(engine: Engine, provider_name: str, update: StatusUpdate) -> bool:
    """Returns True if the message moved forward."""
    with admin_session(engine) as s:
        ref = s.execute(
            text(
                "SELECT tenant_id, message_id FROM message_ref "
                "WHERE provider = :p AND provider_msg_id = :id"
            ),
            {"p": provider_name, "id": update.provider_msg_id},
        ).first()
    if ref is None:
        return False
    tenant_id, message_id = ref[0], ref[1]
    new = MessageStatus(update.status)
    with tenant_session(engine, tenant_id) as s:
        row = s.execute(
            text("SELECT status FROM message WHERE id = :id FOR UPDATE"), {"id": message_id}
        ).first()
        if row is None or not status_advances(str(row[0]), new.value):
            return False
        column = _TS_COLUMN[new]
        extra = ", delivered_at = coalesce(delivered_at, :at)" if new is MessageStatus.READ else ""
        error = ", error = :error" if new is MessageStatus.FAILED else ""
        s.execute(
            text(
                f"UPDATE message SET status = :st, {column} = coalesce({column}, :at)"
                f"{extra}{error} WHERE id = :id"
            ),
            {"st": new.value, "at": update.at, "error": update.error, "id": message_id},
        )
    return True


def _first_time(engine: Engine, provider_name: str, provider_msg_id: str) -> bool:
    with admin_session(engine) as s:
        row = s.execute(
            text(
                "INSERT INTO inbound_event (provider, provider_msg_id) VALUES (:p, :id) "
                "ON CONFLICT DO NOTHING RETURNING provider_msg_id"
            ),
            {"p": provider_name, "id": provider_msg_id},
        ).first()
    return row is not None


def apply_inbound(engine: Engine, provider: WhatsAppProvider, msg: InboundMessage) -> int:
    """Returns the number of tenants opted out by this message."""
    if not is_stop(msg.text) or not _first_time(engine, provider.name, msg.provider_msg_id):
        return 0
    with admin_session(engine) as s:
        targets = customers.tenants_for_phone(s, msg.from_e164)
    opted_out = 0
    for tenant_id, customer_id in targets:
        with tenant_session(engine, tenant_id) as s:
            if customers.append_consent(
                s, tenant_id, customer_id, ConsentStatus.OPTED_OUT, ConsentSource.STOP_REPLY.value,
                note="WhatsApp reply",
            ):  # fmt: skip
                opted_out += 1
    if targets:
        try:
            provider.send_text(msg.from_e164, STOP_CONFIRMATION)
        except ProviderError as exc:  # best effort; the opt-out itself is recorded
            logger.warning("stop confirmation failed", extra={"error": str(exc)[:200]})
    logger.info("stop reply", extra={"phone": mask_phone(msg.from_e164), "tenants": len(targets),
                                     "opted_out": opted_out})  # fmt: skip
    return opted_out


def process(engine: Engine, provider: WhatsAppProvider, body: Any) -> dict[str, int]:
    counts = {"statuses": 0, "updated": 0, "inbound": 0, "opted_out": 0}
    for event in provider.parse_webhook(body):
        if isinstance(event, StatusUpdate):
            counts["statuses"] += 1
            counts["updated"] += int(apply_status(engine, provider.name, event))
        else:
            counts["inbound"] += 1
            counts["opted_out"] += apply_inbound(engine, provider, event)
    return counts
