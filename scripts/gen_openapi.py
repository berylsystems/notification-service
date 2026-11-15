"""Generate the OpenAPI 3.1 spec for the notification service from the Pydantic schemas.

Run: ``uv run python scripts/gen_openapi.py`` -> ``openapi/openapi.json`` (consumed by Orval).
``tests/test_openapi.py`` fails if a route in ``api/app.py`` is missing here (or vice versa).
The SQS contract (not HTTP) is documented in ``docs/NOTIFY_CONTRACT.md``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic.json_schema import models_json_schema
from sg_commons.errors import Problem

from notification_service.api import schemas as s

_ROOT = Path(__file__).resolve().parents[1]
_OUT = _ROOT / "openapi" / "openapi.json"

_MODELS = [
    s.MessageListResponse,
    s.CustomerListResponse,
    s.CustomerDetail,
    s.OptOutRequest,
    s.TemplateListResponse,
    s.TemplateUpsertRequest,
    s.TemplateOut,
    s.NotifySettingsModel,
    s.CampaignCreateRequest,
    s.CampaignUpdateRequest,
    s.CampaignOut,
    s.CampaignListResponse,
    s.CampaignSendRequest,
    s.EstimateResponse,
    s.WebhookAck,
    Problem,
]

_PROBLEM = {
    "description": "Error (RFC 7807)",
    "content": {"application/problem+json": {"schema": {"$ref": "#/components/schemas/Problem"}}},
}
_ADMIN = [{"bearerAuth": []}]  # system-user token (aud sg-platform)
_PROVIDER: list[dict[str, list[str]]] = []  # provider webhook: signature / verify token
_PUBLIC: list[dict[str, list[str]]] = []  # no auth (signed link token in the path)


def _op(
    op_id: str,
    summary: str,
    *,
    tag: str,
    security: list[dict[str, list[str]]],
    params: list[str] = (),  # type: ignore[assignment]
    string_params: list[str] = (),  # type: ignore[assignment]
    query: list[str] = (),  # type: ignore[assignment]
    headers: list[str] = (),  # type: ignore[assignment]
    body: str | None = None,
    response: str | None = None,
    status: str = "200",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "operationId": op_id,
        "summary": summary,
        "tags": [tag],
        "security": security,
    }
    parameters = (
        [
            {"name": p, "in": "path", "required": True,
             "schema": {"type": "string"} if p == "provider"
             else {"type": "string", "format": "uuid"}}
            for p in params
        ]
        + [{"name": p, "in": "path", "required": True, "schema": {"type": "string"}}
           for p in string_params]
        + [{"name": q, "in": "query", "required": False, "schema": {"type": "string"}}
           for q in query]
        + [{"name": h, "in": "header", "required": False, "schema": {"type": "string"}}
           for h in headers]
    )  # fmt: skip
    if parameters:
        out["parameters"] = parameters
    if body:
        out["requestBody"] = {
            "required": True,
            "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{body}"}}},
        }
    ok: dict[str, Any] = {"description": "OK"}
    if response:
        ok["content"] = {
            "application/json": {"schema": {"$ref": f"#/components/schemas/{response}"}}
        }
    out["responses"] = {
        status: ok,
        **(extra or {}),
        "401": _PROBLEM,
        "403": _PROBLEM,
        "404": _PROBLEM,
        "409": _PROBLEM,
        "422": _PROBLEM,
    }
    return out


def _build_paths() -> dict[str, Any]:
    W, M, C, T, K = "webhooks", "messages", "customers", "templates", "campaigns"  # noqa: N806
    return {
        "/webhooks/whatsapp/{provider}": {
            "get": _op("verifyWhatsAppWebhook", "Provider subscription handshake (echo "
                       "hub.challenge when hub.verify_token matches; text/plain)", tag=W,
                       security=_PROVIDER, params=["provider"],
                       query=["hub.mode", "hub.verify_token", "hub.challenge"]),
            "post": _op("receiveWhatsAppWebhook", "Delivery statuses (forward-only) and inbound "
                        "STOP replies; X-Hub-Signature-256 over the raw body", tag=W,
                        security=_PROVIDER, params=["provider"],
                        headers=["X-Hub-Signature-256"], response="WebhookAck"),
        },
        "/notify/messages": {
            "get": _op("listMessages", "Message log, newest first, phones masked (notify:read)",
                       tag=M, security=_ADMIN,
                       query=["purpose", "status", "outlet_id", "campaign_id", "from", "to",
                              "cursor", "limit"],
                       response="MessageListResponse"),
        },
        "/notify/customers": {
            "get": _op("listCustomers", "Customers with current consent (notify:read)", tag=C,
                       security=_ADMIN, query=["consent", "phone", "cursor", "limit"],
                       response="CustomerListResponse"),
        },
        "/notify/customers/{customerId}": {
            "get": _op("getCustomer", "Customer + consent history (notify:read)", tag=C,
                       security=_ADMIN, params=["customerId"], response="CustomerDetail"),
        },
        "/notify/customers/{customerId}/opt-out": {
            "post": _op("optOutCustomer", "Append an ADMIN opt-out (notify:write)", tag=C,
                        security=_ADMIN, params=["customerId"], body="OptOutRequest",
                        response="CustomerDetail"),
        },
        "/notify/templates": {
            "get": _op("listTemplates", "Platform + tenant template mappings (notify:read)",
                       tag=T, security=_ADMIN, response="TemplateListResponse"),
            "put": _op("putTemplate", "Map a code (+language) to a provider template "
                       "(notify:write)", tag=T, security=_ADMIN, body="TemplateUpsertRequest",
                       response="TemplateOut"),
        },
        "/notify/settings": {
            "get": _op("getNotifySettings", "Tenant settings (notify:read)", tag=T,
                       security=_ADMIN, response="NotifySettingsModel"),
            "put": _op("putNotifySettings", "Update tenant settings (notify:write)", tag=T,
                       security=_ADMIN, body="NotifySettingsModel",
                       response="NotifySettingsModel"),
        },
        "/notify/campaigns": {
            "get": _op("listCampaigns", "Campaigns, newest first (notify:read)", tag=K,
                       security=_ADMIN, response="CampaignListResponse"),
            "post": _op("createCampaign", "Create a DRAFT campaign (notify:write)", tag=K,
                        security=_ADMIN, body="CampaignCreateRequest", response="CampaignOut",
                        status="201"),
        },
        "/notify/campaigns/{campaignId}": {
            "get": _op("getCampaign", "One campaign with counts (notify:read)", tag=K,
                       security=_ADMIN, params=["campaignId"], response="CampaignOut"),
            "put": _op("updateCampaign", "Edit a DRAFT campaign (notify:write; 409 otherwise)",
                       tag=K, security=_ADMIN, params=["campaignId"],
                       body="CampaignUpdateRequest", response="CampaignOut"),
        },
        "/notify/campaigns/{campaignId}/estimate": {
            "post": _op("estimateCampaign", "Recipients x (rate + markup) + 18% GST, in paise "
                        "(notify:read)", tag=K, security=_ADMIN, params=["campaignId"],
                        response="EstimateResponse"),
        },
        "/notify/campaigns/{campaignId}/send": {
            "post": _op("sendCampaign", "Send now or at scheduled_at (notify:write)", tag=K,
                        security=_ADMIN, params=["campaignId"], body="CampaignSendRequest",
                        response="CampaignOut"),
        },
        "/notify/campaigns/{campaignId}/cancel": {
            "post": _op("cancelCampaign", "Cancel a DRAFT / SCHEDULED / SENDING campaign "
                        "(notify:write)", tag=K, security=_ADMIN, params=["campaignId"],
                        response="CampaignOut"),
        },
    }  # fmt: skip


def build_spec() -> dict[str, Any]:
    _, top = models_json_schema(
        [(m, "validation") for m in _MODELS], ref_template="#/components/schemas/{model}"
    )
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "SG Notification Service",
            "version": "1.0.0",
            "description": "WhatsApp message log, customers + consent, templates, campaigns and "
            "provider webhooks. Sending is via SQS (see docs/NOTIFY_CONTRACT.md).",
        },
        "servers": [{"url": "https://notify.sgtvl.rest"}],
        "security": [],
        "paths": _build_paths(),
        "components": {
            "securitySchemes": {
                "bearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"},
            },
            "schemas": top.get("$defs", {}),
        },
    }


def main() -> None:
    _OUT.parent.mkdir(parents=True, exist_ok=True)
    _OUT.write_text(json.dumps(build_spec(), indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {_OUT}")


if __name__ == "__main__":
    main()
