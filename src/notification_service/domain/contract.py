"""The SQS ``sg-notify-send`` message contract (v1). See ``docs/NOTIFY_CONTRACT.md``.

Two message types, discriminated by ``type`` (absent = ``SEND`` for leniency):

* ``SEND``    -- send one template message (BILL / PROMO / OTP).
* ``CONSENT`` -- upsert a customer and append a marketing-consent change.

Unknown fields are ignored (producers can add fields before the consumer knows them); a ``v``
other than 1 is rejected.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .enums import E164_RE, TEMPLATE_CODE_RE, ConsentSource, ConsentStatus, Purpose

Phone = str


class _Msg(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ButtonParam(_Msg):
    type: Literal["URL", "COPY_CODE"]
    value: str = Field(min_length=1, max_length=2000)


class SendMessage(_Msg):
    v: Literal[1]
    type: Literal["SEND"] = "SEND"
    purpose: Purpose
    tenant_id: UUID
    outlet_id: UUID | None = None
    to: Phone = Field(pattern=E164_RE)
    template_code: str = Field(pattern=TEMPLATE_CODE_RE)
    language: str = Field(default="en", min_length=2, max_length=10)
    # Ordered body variables ({{1}}, {{2}}, ...); a dict means named variables.
    params: list[str] | dict[str, str] = Field(default_factory=list)
    buttons: list[ButtonParam] = Field(default_factory=list, max_length=3)
    ref_type: str | None = Field(default=None, max_length=40)
    ref_id: str = Field(min_length=1, max_length=120)
    campaign_id: UUID | None = None
    customer_name: str | None = Field(default=None, max_length=120)


class ConsentMessage(_Msg):
    v: Literal[1]
    type: Literal["CONSENT"]
    tenant_id: UUID
    phone: Phone = Field(pattern=E164_RE)
    name: str | None = Field(default=None, max_length=120)
    status: ConsentStatus
    source: ConsentSource
    outlet_id: UUID | None = None
    actor_id: UUID | None = None
    note: str | None = Field(default=None, max_length=300)


def parse_message(body: dict[str, Any]) -> SendMessage | ConsentMessage:
    """Validate one queue message body (raises ``pydantic.ValidationError``)."""
    if body.get("type", "SEND") == "CONSENT":
        return ConsentMessage.model_validate(body)
    return SendMessage.model_validate(body)
