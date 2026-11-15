"""HTTP API for the notification service (notify.sgtvl.rest).

Two audiences:
* **Providers** (``/webhooks/whatsapp/{provider}``) -- no bearer token; GET is the subscription
  handshake (verify token), POST is authenticated by the provider's signature over the raw body
  (Meta: ``X-Hub-Signature-256``). Delivery statuses + inbound STOP replies.
* **Admins** (``/notify/...``) -- system-user tokens (aud ``sg-platform``) with ``notify:read`` /
  ``notify:write``: message log, customers + consent, templates, settings, campaigns.

Sending is not an HTTP route: producers put SEND / CONSENT messages on SQS ``sg-notify-send``
(``docs/NOTIFY_CONTRACT.md``).
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID

from aws_lambda_powertools import Logger
from aws_lambda_powertools.event_handler import APIGatewayHttpResolver, Response
from pydantic import ValidationError as PydanticValidationError
from sg_commons.config import get_settings
from sg_commons.errors import AppError, NotFoundError, UnauthorizedError, ValidationError
from sg_commons.security import AuthContext, authenticate, require_permission

from ..config import get_notify_settings
from ..domain import enums as e
from ..providers.base import WebhookVerificationError
from ..services import admin, campaigns, webhooks
from . import deps
from .schemas import (
    CampaignCreateRequest,
    CampaignListResponse,
    CampaignOut,
    CampaignSendRequest,
    CampaignUpdateRequest,
    CustomerDetail,
    CustomerListResponse,
    EstimateResponse,
    MessageListResponse,
    NotifySettingsModel,
    OptOutRequest,
    TemplateListResponse,
    TemplateOut,
    TemplateUpsertRequest,
    WebhookAck,
)

app = APIGatewayHttpResolver()
logger = Logger(service="notification-service", child=True)
_JSON = "application/json"


def _header(name: str) -> str | None:
    value: str | None = app.current_event.headers.get(name)
    return value


def _admin(permission: str) -> AuthContext:
    ctx = authenticate(_header("authorization"), deps.get_signer(), get_settings())
    require_permission(ctx, permission)
    return ctx


def _uuid(value: str, what: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise ValidationError(f"invalid {what}") from exc


def _query(name: str) -> str | None:
    value: str | None = app.current_event.get_query_string_value(name=name, default_value=None)
    return value or None


def _csv(name: str, allowed: set[str]) -> list[str] | None:
    raw = _query(name)
    if raw is None:
        return None
    values = [v.strip().upper() for v in raw.split(",") if v.strip()]
    bad = [v for v in values if v not in allowed]
    if bad:
        raise ValidationError(f"invalid {name}: {', '.join(bad)}")
    return values


def _datetime(name: str, *, end_of_day: bool = False) -> datetime | None:
    """ISO date (``2026-11-15``: start of day UTC, or the next day for ``to``) or datetime."""
    raw = _query(name)
    if raw is None:
        return None
    try:
        if len(raw) == 10:
            dt = datetime.combine(date.fromisoformat(raw), time.min, tzinfo=UTC)
            return dt + timedelta(days=1) if end_of_day else dt
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError as exc:
        raise ValidationError(f"{name} must be an ISO date or date-time") from exc


def _limit() -> int:
    raw = _query("limit")
    try:
        value = int(raw) if raw else 50
    except ValueError as exc:
        raise ValidationError("limit must be an integer") from exc
    if not 1 <= value <= 200:
        raise ValidationError("limit must be between 1 and 200")
    return value


def _body() -> Any:
    return app.current_event.json_body


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# =============================================================================================
# Provider webhooks
# =============================================================================================


def _provider_for(path_provider: str) -> Any:
    provider = deps.get_provider()
    if path_provider.lower() != provider.name:
        raise NotFoundError("unknown provider")
    return provider


@app.get("/webhooks/whatsapp/<provider>")
def webhook_verify(provider: str) -> Response[str]:
    adapter = _provider_for(provider)
    query = dict(app.current_event.query_string_parameters or {})
    try:
        challenge = adapter.verify_challenge(query)
    except WebhookVerificationError as exc:
        raise UnauthorizedError("webhook verification failed") from exc
    return Response(status_code=200, content_type="text/plain", body=challenge)


@app.post("/webhooks/whatsapp/<provider>")
def webhook_receive(provider: str) -> dict[str, object]:
    adapter = _provider_for(provider)
    event = app.current_event
    raw_text = event.get("body") or ""
    raw = base64.b64decode(raw_text) if event.get("isBase64Encoded") else raw_text.encode()
    try:
        adapter.verify_webhook(dict(event.headers), raw, dict(event.query_string_parameters or {}))
    except WebhookVerificationError as exc:
        logger.warning("webhook signature rejected")
        raise UnauthorizedError("webhook verification failed") from exc
    try:
        body = json.loads(raw or b"{}")
    except ValueError as exc:
        raise ValidationError("body is not JSON") from exc
    counts = webhooks.process(deps.get_engine(), adapter, body)
    return WebhookAck(counts=counts).model_dump(mode="json")


# =============================================================================================
# Admin: messages, customers, templates, settings
# =============================================================================================


@app.get("/notify/messages")
def admin_list_messages() -> dict[str, object]:
    ctx = _admin(e.NOTIFY_READ)
    outlet, campaign = _query("outlet_id"), _query("campaign_id")
    out = admin.list_messages(
        deps.get_engine(),
        ctx.tenant_id,
        purpose=_csv("purpose", {p.value for p in e.Purpose}),
        status=_csv("status", {s.value for s in e.MessageStatus}),
        outlet_id=_uuid(outlet, "outlet_id") if outlet else None,
        campaign_id=_uuid(campaign, "campaign_id") if campaign else None,
        date_from=_datetime("from"),
        date_to=_datetime("to", end_of_day=True),
        cursor=_query("cursor"),
        limit=_limit(),
    )
    return MessageListResponse.model_validate(out).model_dump(mode="json")


@app.get("/notify/customers")
def admin_list_customers() -> dict[str, object]:
    ctx = _admin(e.NOTIFY_READ)
    consent = _csv("consent", {"OPTED_IN", "OPTED_OUT", "NONE"})
    phone = _query("phone")
    if phone:  # a literal "+" in a query string may arrive decoded as a space
        phone = "+" + phone.strip().lstrip("+")
        if not e.is_e164(phone):
            raise ValidationError("phone must be E.164 (+919876543210)")
    out = admin.list_customers(
        deps.get_engine(),
        ctx.tenant_id,
        consent=consent[0] if consent else None,
        phone=phone,
        cursor=_query("cursor"),
        limit=_limit(),
    )
    return CustomerListResponse.model_validate(out).model_dump(mode="json")


@app.get("/notify/customers/<customer_id>")
def admin_get_customer(customer_id: str) -> dict[str, object]:
    ctx = _admin(e.NOTIFY_READ)
    out = admin.get_customer(deps.get_engine(), ctx.tenant_id, _uuid(customer_id, "customer id"))
    return CustomerDetail.model_validate(out).model_dump(mode="json")


@app.post("/notify/customers/<customer_id>/opt-out")
def admin_opt_out(customer_id: str) -> dict[str, object]:
    ctx = _admin(e.NOTIFY_WRITE)
    body = OptOutRequest.model_validate(_body() or {})
    out = admin.opt_out(
        deps.get_engine(), ctx.tenant_id, _uuid(customer_id, "customer id"), ctx.user_id, body.note
    )
    return CustomerDetail.model_validate(out).model_dump(mode="json")


@app.get("/notify/templates")
def admin_list_templates() -> list[object]:
    ctx = _admin(e.NOTIFY_READ)
    rows = admin.list_templates(deps.get_engine(), ctx.tenant_id)
    out: list[object] = TemplateListResponse.model_validate(rows).model_dump(mode="json")
    return out


@app.put("/notify/templates")
def admin_put_template() -> dict[str, object]:
    ctx = _admin(e.NOTIFY_WRITE)
    body = TemplateUpsertRequest.model_validate(_body())
    out = admin.upsert_template(deps.get_engine(), ctx.tenant_id, body.model_dump(mode="json"))
    return TemplateOut.model_validate(out).model_dump(mode="json")


@app.get("/notify/settings")
def admin_get_settings() -> dict[str, object]:
    ctx = _admin(e.NOTIFY_READ)
    out = admin.get_settings(deps.get_engine(), ctx.tenant_id)
    return NotifySettingsModel.model_validate(out).model_dump(mode="json")


@app.put("/notify/settings")
def admin_put_settings() -> dict[str, object]:
    ctx = _admin(e.NOTIFY_WRITE)
    body = NotifySettingsModel.model_validate(_body())
    out = admin.put_settings(deps.get_engine(), ctx.tenant_id, body.promo_cap_days)
    return NotifySettingsModel.model_validate(out).model_dump(mode="json")


# =============================================================================================
# Admin: campaigns
# =============================================================================================


def _campaign(row: dict[str, Any]) -> dict[str, object]:
    return CampaignOut.model_validate(row).model_dump(mode="json")


@app.get("/notify/campaigns")
def admin_list_campaigns() -> list[object]:
    ctx = _admin(e.NOTIFY_READ)
    rows = campaigns.list_campaigns(deps.get_engine(), ctx.tenant_id)
    out: list[object] = CampaignListResponse.model_validate(rows).model_dump(mode="json")
    return out


@app.post("/notify/campaigns")
def admin_create_campaign() -> Response[str]:
    ctx = _admin(e.NOTIFY_WRITE)
    body = CampaignCreateRequest.model_validate(_body())
    row = campaigns.create_campaign(
        deps.get_engine(), ctx.tenant_id, ctx.user_id, body.model_dump(mode="json")
    )
    return Response(
        status_code=201, content_type=_JSON, body=CampaignOut.model_validate(row).model_dump_json()
    )


@app.get("/notify/campaigns/<campaign_id>")
def admin_get_campaign(campaign_id: str) -> dict[str, object]:
    ctx = _admin(e.NOTIFY_READ)
    row = campaigns.get_campaign(deps.get_engine(), ctx.tenant_id, _uuid(campaign_id, "id"))
    return _campaign(row)


@app.put("/notify/campaigns/<campaign_id>")
def admin_update_campaign(campaign_id: str) -> dict[str, object]:
    ctx = _admin(e.NOTIFY_WRITE)
    body = CampaignUpdateRequest.model_validate(_body())
    row = campaigns.update_campaign(
        deps.get_engine(),
        ctx.tenant_id,
        _uuid(campaign_id, "id"),
        body.model_dump(mode="json", exclude_unset=True),
    )
    return _campaign(row)


@app.post("/notify/campaigns/<campaign_id>/estimate")
def admin_estimate_campaign(campaign_id: str) -> dict[str, object]:
    ctx = _admin(e.NOTIFY_READ)
    out = campaigns.estimate(
        deps.get_engine(),
        ctx.tenant_id,
        _uuid(campaign_id, "id"),
        get_notify_settings().provider_markup_paise,
    )
    return EstimateResponse.model_validate(out).model_dump(mode="json")


@app.post("/notify/campaigns/<campaign_id>/send")
def admin_send_campaign(campaign_id: str) -> dict[str, object]:
    ctx = _admin(e.NOTIFY_WRITE)
    body = CampaignSendRequest.model_validate(_body() or {})
    row = campaigns.send_campaign(
        deps.get_engine(), ctx.tenant_id, _uuid(campaign_id, "id"), body.scheduled_at
    )
    return _campaign(row)


@app.post("/notify/campaigns/<campaign_id>/cancel")
def admin_cancel_campaign(campaign_id: str) -> dict[str, object]:
    ctx = _admin(e.NOTIFY_WRITE)
    row = campaigns.cancel_campaign(deps.get_engine(), ctx.tenant_id, _uuid(campaign_id, "id"))
    return _campaign(row)


# =============================================================================================
# Errors
# =============================================================================================


def _problem_response(ex: AppError) -> Response[str]:
    problem = ex.to_problem()
    return Response(
        status_code=problem.status,
        content_type="application/problem+json",
        body=problem.model_dump_json(),
    )


@app.exception_handler(AppError)  # type: ignore[untyped-decorator]
def handle_app_error(ex: AppError) -> Response[str]:
    return _problem_response(ex)


@app.exception_handler(PydanticValidationError)  # type: ignore[untyped-decorator]
def handle_request_validation(ex: PydanticValidationError) -> Response[str]:
    detail = "; ".join(
        f"{'.'.join(str(p) for p in err['loc']) or 'body'}: {err['msg']}" for err in ex.errors()
    )
    return _problem_response(ValidationError(f"invalid request: {detail}"))
