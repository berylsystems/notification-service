# notification-service: the `sg-notify-send` queue contract (v1)

Producers (order-service e-bills and consent, user-service OTP / password reset, inventory-service
STAFF reminders, this service's campaign scheduler) put JSON messages on the SQS standard queue
**`sg-notify-send`**. The sender Lambda consumes them. Delivery is **at-least-once**, so every
message must be safe to receive twice. The sender guarantees that, as described below.

- **Queue:** `arn:aws:sqs:ap-south-1:659551386596:sg-notify-send`. The name is fixed. The URL is
  `https://sqs.ap-south-1.amazonaws.com/659551386596/sg-notify-send`. Both are also in SSM at
  `/sg/notify/queue-url` and `/sg/notify/queue-arn`.
- **DLQ:** `sg-notify-send-dlq`. A message moves there after 5 receives and is kept for 14 days.
- **Producer IAM:** grant `sqs:SendMessage` on the queue ARN in the producer's own stack. The
  queue uses SQS-managed SSE, so no KMS grant is needed.
- **Body:** one JSON object per message (`SendMessage` or `SendMessageBatch`). Unknown fields are
  ignored. Only `"v": 1` is accepted.

The `type` field picks one of two message types. If `type` is missing, the message is treated as
`SEND`.

## `SEND`: send one WhatsApp template message

```json
{
  "v": 1,
  "type": "SEND",
  "purpose": "OTP",
  "tenant_id": "6f1c…",
  "outlet_id": null,
  "to": "+919876543210",
  "template_code": "OTP",
  "language": "en",
  "params": ["123456"],
  "buttons": [{"type": "COPY_CODE", "value": "123456"}],
  "ref_type": "otp_challenge",
  "ref_id": "0b9e…"
}
```

| field | required | notes |
|---|---|---|
| `v` | yes | `1` |
| `type` | no | `"SEND"` (the default) |
| `purpose` | yes | `BILL`, `PROMO`, `OTP` or `STAFF` |
| `tenant_id` | yes | uuid. Every row is stored under this tenant (RLS). |
| `outlet_id` | no | uuid or `null` (OTP has no outlet) |
| `to` | yes | E.164 phone number with a `+` (`^\+[1-9]\d{7,14}$`) |
| `template_code` | yes | `BILL`, `OTP`, `STAFF_COUNT_DUE`, `STAFF_HANDOVER`, `STAFF_DEDUCTION` or `PROMO_[A-Z0-9_]{1,30}`. Mapped to the provider's template name in `wa_template`. A tenant row overrides the platform row. |
| `language` | no | defaults to `en`. Part of the template lookup. |
| `params` | no | an **ordered list** of body variables (`{{1}}`, `{{2}}`, …). A `{name: value}` object sends named variables instead. |
| `buttons` | no | up to 3 items, each `{"type": "URL", "value": "<dynamic url suffix>"}` or `{"type": "COPY_CODE", "value": "<otp>"}`. They are applied by position: the 1st item fills template button 0, and so on. |
| `ref_type` | no | free text describing what the message is about: `pos_ticket`, `otp_challenge`, `campaign`, … |
| `ref_id` | yes | 1–120 characters. **Part of the idempotency key.** |
| `campaign_id` | no | uuid. Set by the scheduler, which updates the campaign's counters. |
| `customer_name` | no | BILL only. Sets the customer's name if it isn't known yet. |

### Per-purpose conventions

| purpose | template_code | params | buttons | ref |
|---|---|---|---|---|
| BILL | `BILL` (UTILITY) | `[store name, bill no, total]` | `[{"type":"URL","value":"<bill token>"}]` | `pos_ticket` / ticket id |
| OTP | `OTP` (AUTHENTICATION) | `[code]` | `[{"type":"COPY_CODE","value":"<code>"}]` | `otp_challenge` / challenge id. Each challenge has its own id, so a resend is a new message. |
| PROMO | `PROMO_*` (MARKETING) | the campaign's params | the campaign's buttons | `campaign` / `"<campaign_id>:<customer_id>"` |
| STAFF | `STAFF_COUNT_DUE` / `STAFF_HANDOVER` (UTILITY) | `[restaurant, kit name]` | none | `custody_kit` / `"<kind>:<kit_id>:<YYYY-MM-DD>"` (one per kit per day) |
| STAFF | `STAFF_DEDUCTION` (UTILITY) | `[restaurant, item, amount]` | none | `custody_kit` / `"DEDUCTION:<kit_id>:<YYYY-MM-DD>"` |

STAFF messages go to employees (inventory-service reminders). Like BILL they skip the marketing
consent check and the PROMO frequency cap. The three STAFF templates are platform rows seeded
`PENDING` (`sg_staff_count_due`, `sg_staff_handover`, `sg_staff_deduction`); tenants can't
override them.

### What the sender does

1. **Idempotency.** The key is `(tenant_id, purpose, ref_id, to)`. A message whose key already
   reached `SENT` or a later status (`DELIVERED`, `READ`, `FAILED`, `SKIPPED`) is dropped. A
   message still `QUEUED` is the retry of a transient failure, so it is attempted again. A BILL
   counts one customer visit, once.
2. **OTP redaction.** For `OTP`, the stored `params` and button `value`s are replaced with
   `"******"`. The real code is used only in memory for the provider call. Logs mask phone numbers
   (`+91******3210`) and never contain codes.
3. **Template check.** If the template is missing, the message becomes `FAILED` with
   `skip_reason=TEMPLATE_NOT_FOUND`. If the template is not `APPROVED`, it becomes `FAILED` with
   `TEMPLATE_NOT_APPROVED`.
4. **PROMO rules.** The message is `SKIPPED` with a reason in any of these cases:
   - `NO_CONSENT`: the customer's latest consent is not `OPTED_IN`.
   - `FREQUENCY_CAP`: another PROMO reached this number within `promo_cap_days` (7 by default,
     set per tenant; 0 turns the cap off).
   - `CAMPAIGN_CANCELLED`: the campaign was cancelled.
5. **Send.** On success the message becomes `SENT` and stores the `provider_msg_id`.
   - **Transient** errors (timeout, 5xx, 429 or throttling codes, expired token): the attempt and
     error are recorded and the record is returned to SQS, which retries with the visibility
     timeout. After 5 receives the message goes to the DLQ.
   - **Permanent** errors (invalid number, template mismatch, …): the message becomes `FAILED`.
6. **Statuses** then move forward only, driven by webhooks:
   - `SENT` → `DELIVERED` → `READ`.
   - `FAILED` can follow only `QUEUED` or `SENT`.
   - Late or out-of-order webhooks never move a status backwards.

A malformed body (bad JSON, failed validation, `v` ≠ 1) raises an error. It is retried and then
lands in the DLQ, where you can inspect it.

## `CONSENT`: upsert a customer and record a marketing-consent change

```json
{
  "v": 1,
  "type": "CONSENT",
  "tenant_id": "6f1c…",
  "phone": "+919876543210",
  "name": "Ravi",
  "status": "OPTED_IN",
  "source": "POS",
  "outlet_id": "a1b2…",
  "actor_id": "employee uuid",
  "note": null
}
```

| field | required | notes |
|---|---|---|
| `status` | yes | `OPTED_IN` or `OPTED_OUT` |
| `source` | yes | `POS`, `ADMIN` or `IMPORT` (`STOP_REPLY` is reserved for the webhook) |
| `name`, `outlet_id`, `actor_id`, `note` | no | `actor_id` is the employee or admin who recorded it |

- The customer is created if needed. Its name and first-seen outlet are filled only if they are
  empty. A CONSENT message does not count a visit.
- A row is appended to the append-only `customer_consent` table only if `status` differs from the
  current state, so redeliveries are no-ops.
- Opt-in happens only through POS, ADMIN or IMPORT. An inbound `START` reply never opts anyone in.

## Inbound replies (webhook, for reference)

A reply of `STOP`, `STOP ALL`, `UNSUBSCRIBE` or `stop promotions` (case-insensitive, including
the template's quick-reply button) does the following:

- It appends an `OPTED_OUT` / `STOP_REPLY` row in **every tenant** where the number is a customer.
  The sending number is shared, so the guest can't tell restaurants apart.
- It sends one confirmation text. This is free inside the 24-hour window.
- Provider retries of the same inbound message are de-duplicated.
