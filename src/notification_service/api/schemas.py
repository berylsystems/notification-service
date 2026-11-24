"""Pydantic request/response models (also the OpenAPI components; see scripts/gen_openapi.py).

Phones in responses are always masked (``+91******3210``). Paise amounts are decimal strings.
Lists are cursor-paginated: ``{"items": [...], "next_cursor": "..." | null}``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, RootModel

from ..domain.contract import ButtonParam
from ..domain.enums import (
    CampaignStatus,
    ConsentSource,
    ConsentStatus,
    MessageStatus,
    Purpose,
    TemplateCategory,
    TemplateStatus,
)

Params = list[Annotated[str, Field(max_length=1000)]]


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


# ---- messages ----


class MessageOut(_Model):
    id: UUID
    outlet_id: UUID | None = None
    customer_id: UUID | None = None
    phone: str = Field(description="masked")
    purpose: Purpose
    template_code: str
    params: list[str] | dict[str, str] = Field(description="OTP values are stored as ******")
    ref_type: str | None = None
    ref_id: str
    campaign_id: UUID | None = None
    status: MessageStatus
    skip_reason: str | None = None
    provider: str | None = None
    error: str | None = None
    category: TemplateCategory | None = None
    attempts: int
    created_at: AwareDatetime
    sent_at: AwareDatetime | None = None
    delivered_at: AwareDatetime | None = None
    read_at: AwareDatetime | None = None
    failed_at: AwareDatetime | None = None


class MessageListResponse(_Model):
    items: list[MessageOut]
    next_cursor: str | None = None


# ---- customers ----


class ConsentOut(_Model):
    id: int
    status: ConsentStatus
    source: ConsentSource
    outlet_id: UUID | None = None
    actor_id: UUID | None = None
    note: str | None = None
    at: AwareDatetime


class CustomerOut(_Model):
    id: UUID
    phone: str = Field(description="masked")
    name: str | None = None
    first_seen_outlet_id: UUID | None = None
    last_outlet_id: UUID | None = None
    last_visit_at: AwareDatetime | None = None
    visit_count: int
    created_at: AwareDatetime
    consent: ConsentStatus | None = Field(default=None, description="latest; null = never asked")
    consent_at: AwareDatetime | None = None


class CustomerDetail(CustomerOut):
    history: list[ConsentOut] = Field(default_factory=list, description="newest first")


class CustomerListResponse(_Model):
    items: list[CustomerOut]
    next_cursor: str | None = None


class OptOutRequest(_Model):
    note: str | None = Field(default=None, max_length=300)


# ---- templates ----


class TemplateUpsertRequest(_Model):
    """Tenant mapping of a promotion template. BILL / OTP / STAFF_* are platform templates on the
    shared number and can't be overridden per tenant (they're managed by the platform operator)."""

    code: str = Field(pattern=r"^PROMO_[A-Z0-9_]{1,30}$", examples=["PROMO_WEEKEND"])
    provider_template_name: str = Field(min_length=1, max_length=512)
    category: Literal[TemplateCategory.MARKETING] = TemplateCategory.MARKETING
    language: str = Field(default="en", min_length=2, max_length=10)
    variables: list[str] = Field(default_factory=list, max_length=20)
    status: TemplateStatus = TemplateStatus.PENDING


class TemplateOut(_Model):
    id: UUID
    scope: str = Field(description="PLATFORM (shared number, read-only) or TENANT")
    code: str
    provider_template_name: str
    category: TemplateCategory
    language: str
    variables: list[str]
    status: TemplateStatus
    updated_at: AwareDatetime


class TemplateListResponse(RootModel[list[TemplateOut]]):
    pass


# ---- settings ----


class NotifySettingsModel(_Model):
    promo_cap_days: int = Field(
        default=7, ge=0, le=365, description="max 1 promo per customer per N days (0 = no cap)"
    )


# ---- campaigns ----


class _CampaignFields(_Model):
    name: str = Field(min_length=1, max_length=120)
    template_code: str = Field(pattern=r"^PROMO_[A-Z0-9_]{1,30}$")
    language: str = Field(default="en", min_length=2, max_length=10)
    params: Params = Field(default_factory=list, max_length=20)
    buttons: list[ButtonParam] = Field(default_factory=list, max_length=3)
    outlet_ids: list[UUID] | None = Field(default=None, description="null = all stores")
    visited_within_days: int | None = Field(default=None, ge=1, le=3650)
    scheduled_at: AwareDatetime | None = None


class CampaignCreateRequest(_CampaignFields):
    pass


class CampaignUpdateRequest(_Model):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    template_code: str | None = Field(default=None, pattern=r"^PROMO_[A-Z0-9_]{1,30}$")
    language: str | None = Field(default=None, min_length=2, max_length=10)
    params: Params | None = Field(default=None, max_length=20)
    buttons: list[ButtonParam] | None = Field(default=None, max_length=3)
    outlet_ids: list[UUID] | None = None
    visited_within_days: int | None = Field(default=None, ge=1, le=3650)
    scheduled_at: AwareDatetime | None = None


class CampaignOut(_Model):
    id: UUID
    name: str
    template_code: str
    language: str
    params: list[str]
    buttons: list[ButtonParam]
    outlet_ids: list[UUID] | None = None
    visited_within_days: int | None = None
    scheduled_at: AwareDatetime | None = None
    status: CampaignStatus
    recipients: int
    sent: int
    failed: int
    skipped: int
    est_cost_paise: int | None = None
    created_by: UUID | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class CampaignListResponse(RootModel[list[CampaignOut]]):
    pass


class CampaignSendRequest(_Model):
    scheduled_at: AwareDatetime | None = Field(default=None, description="omit = send now")


class EstimateResponse(_Model):
    campaign_id: UUID
    category: TemplateCategory
    recipients: int
    rate_paise: Decimal
    markup_paise: Decimal
    subtotal_paise: Decimal
    gst_paise: Decimal = Field(description="18% GST")
    total_paise: Decimal


class WebhookAck(_Model):
    ok: bool = True
    counts: dict[str, Any] = Field(default_factory=dict)
