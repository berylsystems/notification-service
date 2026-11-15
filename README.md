# SG notification service (`notify.sgtvl.rest`)

WhatsApp messaging for the platform:

- **e-bills** after payment
- **promotional campaigns** to customers who opted in
- **OTP** sign-in codes

Everything goes through one shared platform number, behind a **provider-neutral interface**. Meta
Cloud API is implemented now. Gupshup, Karix, Route Mobile and Kaleyra each need only one adapter
in `providers/` plus a config change.

- **Queue contract** (what producers send): [`docs/NOTIFY_CONTRACT.md`](docs/NOTIFY_CONTRACT.md)
- **HTTP spec:** `openapi/openapi.json`

```
order-service / user-service / campaign scheduler ─▶ SQS sg-notify-send (+ DLQ after 5)
                                                         │  batch 10, reserved concurrency 5
                                                         ▼
                                           sender Lambda ─▶ WhatsAppProvider (meta | fake | …)
provider webhooks ─▶ POST /webhooks/whatsapp/{provider}  (statuses forward-only, STOP → opt-out)
EventBridge rate(1 min) ─▶ scheduler Lambda (due campaigns → PROMO SEND messages, pages of 500)
```

## Routes

**Webhook routes** take no bearer token. **Admin routes** take a **user token** (aud
`sg-platform`) with `notify:read` or `notify:write`. Phone numbers are always masked in responses.
Errors are RFC 7807. Lists use cursors: `?cursor=&limit=` returns `{items, next_cursor}`.

| Route | Auth | Notes |
|---|---|---|
| `GET /webhooks/whatsapp/{provider}` | verify token | Meta handshake: echoes `hub.challenge` (text/plain) |
| `POST /webhooks/whatsapp/{provider}` | `X-Hub-Signature-256` | HMAC-SHA256 of the raw body with `app_secret`, compared in constant time. Handles statuses and inbound STOP. |
| `GET /notify/messages?purpose=&status=&outlet_id=&campaign_id=&from=&to=` | `notify:read` | message log. OTP params appear as `******`. |
| `GET /notify/customers?consent=OPTED_IN\|OPTED_OUT\|NONE&phone=` | `notify:read` | customers with their latest consent |
| `GET /notify/customers/{id}` | `notify:read` | consent history, newest first |
| `POST /notify/customers/{id}/opt-out` | `notify:write` | `{note}` → appends an `ADMIN` opt-out |
| `GET/PUT /notify/templates` | read / write | maps a code (+ language) to the provider's template name, category and status. Platform rows (BILL, OTP) are read-only. |
| `GET/PUT /notify/settings` | read / write | `{promo_cap_days}` (default 7; 0 = no cap) |
| `GET/POST /notify/campaigns`, `GET/PUT /notify/campaigns/{id}` | read / write | PUT works only while the campaign is a DRAFT |
| `POST /notify/campaigns/{id}/estimate` | `notify:read` | recipients × (rate + markup) + 18% GST, in paise |
| `POST /notify/campaigns/{id}/send` | `notify:write` | `{scheduled_at?}`. Without it, the campaign goes out within a minute. |
| `POST /notify/campaigns/{id}/cancel` | `notify:write` | from DRAFT, SCHEDULED or SENDING. Messages already queued are skipped. |

### Prices used by the estimate

Rates are in paise per message, before GST (Meta India, from 1 Oct 2026):

| category | rate |
|---|---|
| MARKETING | 86.31 |
| UTILITY | 11.5 |
| AUTHENTICATION | 11.5 |

- A provider markup is added from `SG_NOTIFY_PROVIDER_MARKUP_PAISE` (default 0).
- GST is 18%, rounded half-up to the paisa.

## Data

Tenant data lives under **Row-Level Security** on `tenant_id`, in the shared Aurora database
(role `sg_notify`, version table `alembic_version_notify`):

- **`customer`:** E.164 phone, unique per tenant; visits counted from BILLs.
- **`customer_consent`:** append-only (trigger plus grants); the latest row is the current state.
- **`wa_template`:** rows with `tenant_id` NULL are platform-wide. Every tenant can read them; no
  tenant can write them.
- **`message`:** unique on `(tenant_id, purpose, ref_id, phone)`.
- **`campaign`**
- **`tenant_notify_settings`**

### Cross-tenant lookups

Three paths have no tenant context: webhook status updates, inbound STOP replies, and the
scheduler. They use small **non-RLS index tables that hold only ids**:

| table | maps | maintained by |
|---|---|---|
| `message_ref` | `provider_msg_id` → tenant and message | the sender |
| `customer_phone_ref` | `sha256(phone)` → tenant and customer (so no phone number sits outside RLS) | a trigger on `customer` |
| `inbound_event` | — | de-duplicates provider webhook retries |
| `campaign_due` | campaign → tenant, due time, 5-minute lease | the scheduler (`FOR UPDATE SKIP LOCKED`) |

After a lookup, every write happens in a tenant-scoped session.

**Why not a SECURITY DEFINER function?** On Aurora the table owner is not a superuser, and
`FORCE ROW LEVEL SECURITY` applies to the owner too. A definer function would still see nothing
without a BYPASSRLS role, which is broader than these id-only tables.

**Templates for the shared number:**
- The migration seeds platform `BILL` (`sg_bill`, UTILITY) and `OTP` (`sg_otp`, AUTHENTICATION)
  with status PENDING.
- Once Meta approves them, set the real names and status as the DB admin, e.g.
  `UPDATE wa_template SET provider_template_name='…', status='APPROVED' WHERE tenant_id IS NULL AND code='BILL';`
- Tenants map their own `PROMO_*` templates through `PUT /notify/templates`.

## Develop

```bash
uv sync
make test      # Postgres testcontainers (Docker)
make lint types
uv run ruff format --check .
uv run python scripts/gen_openapi.py   # -> openapi/openapi.json (tests fail if routes/spec drift)
make requirements                      # Lambda bundle deps (vendors sg-commons)
```

Locally, without `SG_NOTIFY_WHATSAPP_SECRET_NAME`, the **fake** provider is used. A deployed
Lambda (where `SG_KMS_SIGNING_KEY_ID` is set) refuses to start without the secret.

## Deploy (user runs)

**Prerequisites:** SgUserServiceDev must be deployed. It provides the shared DB, the signing key,
the SSM `/sg/shared/*` parameters and the `tenant` table.

```bash
make requirements
cd infra/cdk && python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
AWS_PROFILE=VENKAT npx cdk deploy SgNotificationServiceDev --require-approval never
# then invoke the MigrateFunctionName output once (creates sg_notify + grants)
```

No `-c` context is needed. The SSM lookups are cached in `infra/cdk/cdk.context.json`.

### After deploy

1. **Provider secret.** Open `sg/notification-service/whatsapp`. It was created with placeholders
   and a generated `verify_token`.
   - Fill in `access_token`, `phone_number_id` and `app_secret`.
   - Keep `"provider": "meta"`.
   - Lambdas cache the secret per container. Update the function configuration, or wait for new
     containers, to pick up changes.
2. **Meta webhook.** In the Meta app, set:
   - callback URL: `https://notify.sgtvl.rest/webhooks/whatsapp/meta`
   - verify token: the generated `verify_token`
   - subscriptions: `messages`
3. **Producers.** In each producer's stack, grant `sqs:SendMessage` on
   `arn:aws:sqs:ap-south-1:659551386596:sg-notify-send`.
4. **Admins.** Admins need `notify:read` / `notify:write` on their role. This is a user-service
   follow-up.

**Brand/legal:** WhatsApp templates and promotional campaigns sent to guests are external brand
content. They need brand/legal sign-off before going live.
