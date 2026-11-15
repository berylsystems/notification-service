"""Domain enumerations, permission codes, Meta price constants and small pure helpers
(phone validation / masking, OTP redaction, cost estimate)."""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

NOTIFY_READ = "notify:read"
NOTIFY_WRITE = "notify:write"


class Purpose(StrEnum):
    BILL = "BILL"
    PROMO = "PROMO"
    OTP = "OTP"


class MessageStatus(StrEnum):
    QUEUED = "QUEUED"
    SENT = "SENT"
    DELIVERED = "DELIVERED"
    READ = "READ"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


# Forward-only ordering for provider status updates. FAILED may follow QUEUED/SENT (the provider
# accepted it and then could not deliver) but never DELIVERED/READ; SKIPPED is terminal.
STATUS_RANK = {
    MessageStatus.QUEUED: 0,
    MessageStatus.SENT: 1,
    MessageStatus.FAILED: 2,
    MessageStatus.DELIVERED: 3,
    MessageStatus.READ: 4,
    MessageStatus.SKIPPED: 9,
}
FINAL_STATUSES = frozenset(s for s in MessageStatus if s is not MessageStatus.QUEUED)


def status_advances(current: str, new: str) -> bool:
    """True if a provider update ``current -> new`` moves the message forward."""
    cur, nxt = MessageStatus(current), MessageStatus(new)
    if cur is MessageStatus.SKIPPED or nxt in (MessageStatus.QUEUED, MessageStatus.SKIPPED):
        return False
    if nxt is MessageStatus.FAILED:
        return cur in (MessageStatus.QUEUED, MessageStatus.SENT)
    return STATUS_RANK[nxt] > STATUS_RANK[cur]


class SkipReason(StrEnum):
    NO_CONSENT = "NO_CONSENT"
    FREQUENCY_CAP = "FREQUENCY_CAP"
    TEMPLATE_NOT_FOUND = "TEMPLATE_NOT_FOUND"
    TEMPLATE_NOT_APPROVED = "TEMPLATE_NOT_APPROVED"
    CAMPAIGN_CANCELLED = "CAMPAIGN_CANCELLED"


class ConsentStatus(StrEnum):
    OPTED_IN = "OPTED_IN"
    OPTED_OUT = "OPTED_OUT"


class ConsentSource(StrEnum):
    POS = "POS"
    STOP_REPLY = "STOP_REPLY"
    ADMIN = "ADMIN"
    IMPORT = "IMPORT"


class TemplateCategory(StrEnum):
    MARKETING = "MARKETING"
    UTILITY = "UTILITY"
    AUTHENTICATION = "AUTHENTICATION"


class TemplateStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class CampaignStatus(StrEnum):
    DRAFT = "DRAFT"
    SCHEDULED = "SCHEDULED"
    SENDING = "SENDING"
    DONE = "DONE"
    CANCELLED = "CANCELLED"


TEMPLATE_CODE_RE = r"^(BILL|OTP|PROMO_[A-Z0-9_]{1,30})$"
E164_RE = r"^\+[1-9]\d{7,14}$"
_E164 = re.compile(E164_RE)

# ---- Prices (Meta, India, from 1 Oct 2026), per message in paise, before GST ----
RATE_PAISE = {
    TemplateCategory.MARKETING: Decimal("86.31"),
    TemplateCategory.UTILITY: Decimal("11.5"),
    TemplateCategory.AUTHENTICATION: Decimal("11.5"),
}
GST_RATE = Decimal("0.18")

REDACTED = "******"

# Inbound replies that opt a phone out of marketing (compared after strip + casefold).
STOP_WORDS = frozenset({"stop", "stop all", "unsubscribe", "stop promotions"})


def is_e164(phone: str) -> bool:
    return bool(_E164.match(phone))


def normalize_phone(raw: str) -> str:
    """Provider ``from`` values are digits without ``+`` (Meta: ``919876543210``)."""
    digits = "".join(ch for ch in raw if ch.isdigit())
    return f"+{digits}"


def mask_phone(phone: str | None) -> str:
    """``+919876543210`` -> ``+91******3210``. Never log or return a full number."""
    if not phone:
        return ""
    if len(phone) <= 7:
        return "****" + phone[-2:]
    return phone[:3] + "*" * (len(phone) - 7) + phone[-4:]


def redact_params(params: list[str] | dict[str, str]) -> list[str] | dict[str, str]:
    if isinstance(params, dict):
        return dict.fromkeys(params, REDACTED)
    return [REDACTED for _ in params]


def redact_buttons(buttons: list[dict[str, str]]) -> list[dict[str, str]]:
    return [{**b, "value": REDACTED} if "value" in b else dict(b) for b in buttons]


def estimate_cost(
    recipients: int, category: str, markup_paise: Decimal
) -> dict[str, Decimal | int]:
    """recipients x (Meta rate + provider markup) + 18% GST, in paise (2 dp, half-up)."""
    rate = RATE_PAISE[TemplateCategory(category)]
    per_message = rate + markup_paise
    subtotal = (per_message * recipients).quantize(Decimal("0.01"), ROUND_HALF_UP)
    gst = (subtotal * GST_RATE).quantize(Decimal("0.01"), ROUND_HALF_UP)
    return {
        "recipients": recipients,
        "rate_paise": rate,
        "markup_paise": markup_paise,
        "subtotal_paise": subtotal,
        "gst_paise": gst,
        "total_paise": subtotal + gst,
    }
