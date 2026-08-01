# Database Schema — SQLite (Postgres-ready)

All models use Django's ORM only (no raw SQL, no SQLite-specific types) so the future SQLite →
Postgres move (see `ARCHITECTURE.md` Section 6) is a config change, not a rewrite.

## accounts

**User** (extends Django's `AbstractUser` or a `OneToOne` profile)
- id (pk)
- email (unique)
- auth_provider ("google" | "email")
- google_sub (nullable, unique)
- created_at, updated_at

**Business**
- id (pk)
- owner (FK → User, one business per owner for now — no multi-user)
- business_name
- business_category
- currency_code (default "PKR")
- logo_url (nullable)
- language ("en" | "ur" | "roman_ur")
- gateway_session_id (nullable — the WhatsApp Gateway's session id, set on first connect)
- created_at, updated_at

## customers

**Customer**
- id (pk)
- business (FK → Business)
- name
- phone
- address (nullable, default "")
- opening_balance (Decimal)
- current_balance (Decimal) — denormalized running balance, recalculated on cascade
- note (text, nullable) — one free-text note per customer
- created_at, updated_at

## sales

**ActivityEntry** (single table backs both Sale and Payment rows — matches the Flutter
`ActivityItem` contract exactly, `type` discriminates)
- id (pk)
- business (FK → Business)
- customer (FK → Customer)
- type ("sale" | "payment")
- amount (Decimal) — sale total, or payment amount
- balance_after (Decimal) — running balance immediately after this entry
- timestamp (datetime)
- sale_group_id (UUID, nullable) — links a same-transaction sale+payment pair
- payment_method ("cash"|"bank"|"jazzcash"|"easypaisa", nullable — only for payments)
- note (text, nullable — only for payments)
- created_by ("manual"|"ai_chat") — for auditing which flow created it
- created_at, updated_at

**SaleLineItem**
- id (pk)
- entry (FK → ActivityEntry, only present when entry.type == "sale")
- item_name
- quantity (Decimal)
- rate (Decimal)
  (amount = quantity * rate, computed, not stored)

## chat

**Conversation**
- id (pk)
- business (FK → Business)
- created_at, updated_at

**ChatMessage**
- id (pk)
- conversation (FK → Conversation)
- sender ("owner" | "ai")
- text (nullable)
- speech_text (nullable) — native-script version for TTS, see `backend_workflow.md` Section 4
- image_url (nullable) — set when the owner attached a photo
- draft_bill (JSON, nullable) — {customer_id, previous_balance, total_amount, payment_received}
- draft_confirmed (bool, default False)
- document_ready (JSON, nullable) — {document_type, document_url}
- timestamp

## image_info_extractor

**ExtractionJob** (mirrors a `JobTask` but keeps domain-specific fields queryable)
- id (pk)
- business (FK → Business)
- job_task (FK → jobs.JobTask)
- source_image_url
- extracted_data (JSON, nullable) — raw Gemini Vision output
- resolved_customer (FK → Customer, nullable) — set once matched/confirmed
- status ("pending"|"needs_clarification"|"resolved"|"failed")
- created_at, updated_at

## documents

**GeneratedDocument**
- id (pk)
- business (FK → Business)
- job_task (FK → jobs.JobTask)
- doc_type ("invoice"|"statement"|"receipt"|"report")
- related_entry (FK → sales.ActivityEntry, nullable)
- file_url
- created_at

## jobs

**JobTask**
- id (pk)
- business (FK → Business)
- type ("pdf" | "image_extract")
- status ("queued"|"processing"|"done"|"failed")
- payload (JSON)
- result (JSON, nullable)
- error (text, nullable)
- created_at, started_at, finished_at

## whatsapp (no new tables — uses Business.gateway_session_id; state is fetched live from the Gateway, not cached in Django beyond the session id)

## notifications

**Notification**
- id (pk)
- business (FK → Business)
- type ("invoice_sent"|"payment_received"|"whatsapp_disconnected"|"pending_payment_reminder"|"daily_summary")
- payload (JSON, nullable — e.g. which customer/invoice)
- timestamp
- read (bool, default False)

## billing

**Plan**
- id (pk)
- name ("Basic 400"|"Standard 800"|"Premium 1500"|"Custom")
- price_pkr (Decimal)
- is_custom (bool, default False)
- billing_period ("monthly")
- chat_history_limit (int, default 15) — number of past messages (owner + AI combined) sent to
  Groq per chat call for businesses on this plan; admin-editable in Django admin, no deploy needed
- created_at, updated_at

**PlanFeature**
- id (pk)
- plan (FK → Plan)
- feature_key ("ai_chat"|"image_extraction"|"voice_reply"|"whatsapp_send"|...)
- enabled (bool)
- monthly_cap (int, nullable) — e.g. max AI messages/month, null = unlimited

**Subscription**
- id (pk)
- business (FK → Business, one active subscription per business)
- plan (FK → Plan)
- status ("active"|"expired"|"cancelled")
- started_at, expires_at
- is_manual_override (bool, default False) — set true when admin manually grants/edits this in Django admin
- chat_history_limit_override (int, nullable) — if set, overrides `plan.chat_history_limit` for
  this one business specifically; null = fall back to the plan's value
- notes (text, nullable) — admin's free-text reason for the override

**UsageCounter** (for monthly_cap enforcement)
- id (pk)
- business (FK → Business)
- feature_key
- period_start (date, first of month)
- count (int, default 0)

## Indexing notes
- `ActivityEntry`: index on `(business_id, customer_id, timestamp)` for fast history queries.
- `JobTask`: index on `(status, created_at)` for the worker loop's "pick oldest queued" query.
- `UsageCounter`: unique together on `(business_id, feature_key, period_start)`.

## Migration-to-Postgres notes
- Avoid `django.db.models.functions` calls that behave differently across backends without testing both.
- Use `DecimalField(max_digits=12, decimal_places=2)` everywhere money is stored — behaves identically on both backends.
- JSON fields: use `models.JSONField` (works on both SQLite 3.9+ and Postgres) — never a raw text column you `json.loads` yourself.
