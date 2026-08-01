# Business Logic — Plans, Feature Gating, and Domain Rules

## 1. Plans

| Plan | Price (PKR/month) | Typical features |
|---|---|---|
| Basic | 400 | Manual bookkeeping only (customers, sales, payments, PDFs, reports) — no AI |
| Standard | 800 | Basic + AI Chat (text) |
| Premium | 1500 | Standard + Image/OCR extraction + WhatsApp automation |
| Custom | admin-set | Any combination, hand-configured per business in Django admin |
| (no plan / expired) | 0 | Same as Basic — manual-only, never fully locked out of core bookkeeping |

These are seed data (a Django data migration or fixture), not hardcoded in code — see
`claude_rule.md` Section 6. Prices and feature sets are editable from Django admin without a
deploy.

## 2. Feature keys (the only vocabulary code should check against)

- `ai_chat` — text AI chat
- `voice_reply` — spoken AI replies (depends on ai_chat being on; still a separate flag so a plan
  could offer chat without voice)
- `image_extraction` — photo/receipt OCR chat flow
- `whatsapp_send` — sending invoices/statements/reminders over WhatsApp (Basic-tier businesses can
  still **connect** WhatsApp for their own visibility if desired, but sending through it is gated
  — confirm this scope with product before Milestone 10 if it should be stricter)

`Plan.has_feature(feature_key)` and `Subscription.business_has_feature(feature_key)` are the only
two call sites app code should use. Never inline a plan-name string comparison.

## 3. Enforcement rule
> **Never degrade core manual bookkeeping behind a paywall — only gate the AI/automation surface.**

Concretely: a business on Basic (or with no active subscription at all) must always be able to:
add/edit customers, record sales and payments, view balances/history, generate and download
invoices/statements/receipts/reports as PDFs, use Settings, view Notifications.

A business without a feature enabled must be blocked (with a clear upgrade-prompt error, not a
silent failure) from: AI Chat text/voice, image extraction, and (per the scope note above)
WhatsApp-sent documents.

## 4. Usage caps
Some plans may cap a feature instead of a flat on/off (e.g. "Standard: 200 AI messages/month").
`UsageCounter` (see `sqlite_database_attributes.md`) tracks this per business per feature per
calendar month. On each gated call:
1. Check `has_feature` — if false, 403 `FEATURE_NOT_ON_PLAN`.
2. If true and a `monthly_cap` is set on the `PlanFeature`, check/increment `UsageCounter` — if
   over cap, 429 `USAGE_CAP_EXCEEDED`.
3. Only after both checks pass does the expensive call (Groq/Gemini) happen.

## 4a. Chat history limit (admin-configurable, not hardcoded)
The number of past chat messages (owner + AI combined) sent to Groq per call is read from
`Plan.chat_history_limit` (default 15), optionally overridden per business via
`Subscription.chat_history_limit_override`. Both are editable in Django admin without a deploy —
same pattern as `PlanFeature.monthly_cap`. `apps/chat`'s context-assembly step (Section 2 of
`ai_automation_layer.md`) must resolve this value per-call (override if set, else the business's
plan default) rather than reading a constant.

## 5. Manual admin overrides
`Subscription.is_manual_override` + `notes` exist specifically so you (the business owner running
this SaaS) can hand-adjust a specific customer's plan or grant a temporary custom monthly deal
directly in Django admin, without needing a payments/webhook integration on day one. Payment
collection itself (JazzCash/EasyPaisa/bank transfer confirmation) is assumed manual/off-platform
for now — `billing` just needs to represent the outcome (which plan, until when), not process the
payment. If/when a payment gateway is integrated later, it should write into the same
`Subscription` model, not a parallel one.

## 6. Core domain rules (carried over from the existing Flutter/mock logic — must not regress)

- **Balance formula:** `current_balance = previous_balance + sale_total − amount_received`,
  computed server-side, always. The Flutter app only ever displays what Django returns; it never
  recomputes balances itself.
- **Same-transaction sale+payment:** recording a sale with a simultaneous amount received creates
  two linked `ActivityEntry` rows (`sale` then `payment`, sharing `sale_group_id`, sorted
  sale-then-payment) — not a single combined row.
- **Forward-only balance cascade:** editing/deleting an entry recalculates `balance_after` for all
  *later* entries for that customer; entries before the edit point are untouched.
- **No locking on edit/delete:** any past Sale or Payment can be edited or deleted at any time —
  by explicit product decision, there is no "locked after N days" rule.
- **Draft Bill is never a separate data model.** "Edit Draft"/"Save Draft" only mutates the chat
  message's JSON in place — zero backend writes. "Confirm and Send" is the *only* action that
  calls the real sales-recording path, identical to a manually entered sale.
- **Single owner per business** — no multi-user/staff accounts, by explicit product decision (do
  not add this "for flexibility" without being asked).
