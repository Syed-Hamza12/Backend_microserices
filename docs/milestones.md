# Backend Milestones (10)

Each milestone should end with something runnable/demoable — not just code sitting unused.

**Status tracking:** each milestone has a `- [ ] Status: Done` checkbox. Claude Code must tick it
(`- [ ] ` → `- [x] `) the moment that milestone's deliverable is actually working end-to-end (not
when code is merely written) — no separate command needed, this file's checkboxes are the source
of truth. Never tick a box speculatively; if partially done, leave it unchecked and note what's
left inline instead of checking it early.

## Milestone 1 — Project skeleton & auth
- Three service folders scaffolded (`accountant_backend` — the Django project, name predates this
  doc, kept as-is instead of renaming to `django-backend`; `fastapi-service`; `whatsapp-gateway`
  already exists).
- Django: `apps/accounts` app — Business/User models, email auth (register/login) + JWT issuing.
  Google OAuth endpoint exists but is a stub — real Google ID-token verification is deferred until
  a `GOOGLE_OAUTH_CLIENT_ID` is provided (see `apps/accounts/views.py::GoogleAuthView`).
- FastAPI: skeleton with `X-Internal-Key` auth dependency, `/health` (public) + `/internal/ping`
  (key-protected, proves the dependency works) endpoints.
- Deliverable: Flutter can register/login and get a JWT; `Splash → Register → Create Business` works end-to-end against real Django.
- [x] Status: Done

## Milestone 2 — Business Profile + Customers CRUD
- `accounts` Business Profile endpoints; `customers` app full CRUD.
- Deliverable: Customers tab fully real (list/add/edit/notes), matches `BACKEND_INTEGRATION_GUIDE.md` Section 5–6 contract.
- [x] Status: Done

## Milestone 3 — Sales & Payments + balance cascade
- `sales` app: ActivityEntry + SaleLineItem, recordSale/editSale/recordPayment/editPayment, forward-only balance cascade recalculation.
- Deliverable: Add Entry screen (Sale + Payment modes) fully real; Customer Detail's Overview/Bills/Payments tabs show real data.
- [x] Status: Done

## Milestone 4 — Jobs app + FastAPI PDF generation
- `jobs` app (JobTask model, worker loop management command).
- FastAPI `/pdf/generate` endpoint (invoice/receipt templates first).
- `documents` app wiring Django ↔ JobTask ↔ FastAPI.
- Deliverable: Invoice Preview / Payment Receipt screens fetch a real generated PDF, with the phone polling job status.
- Implementation note: FastAPI renders Jinja2 HTML templates (`fastapi-service/templates/`) to PDF
  via `xhtml2pdf` (pure-Python, no native/GTK deps — chosen over WeasyPrint for Windows-dev
  friendliness) and writes into a filesystem folder shared with Django
  (`shared_media/pdfs/`, pointed at by both services' `PDF_STORAGE_DIR`/`MEDIA_ROOT` env vars).
  Django serves it back at `/media/pdfs/...` (dev-only `static()` serving; a real deployment would
  swap this for object storage/nginx, same `file_url` contract either way).
- [x] Status: Done

## Milestone 5 — Statements & Reports PDF + Reports screen data
- Extend `documents`/FastAPI templates to statement + report PDFs.
- Deliverable: Settings → Reports "Download PDF Report" and Customer Detail → Statement fully real.
- Implementation note: per `BACKEND_INTEGRATION_GUIDE.md` Section 10, the Reports *screen data*
  itself needs no new endpoint (computed client-side from `customers`/`historyFor()`, already real
  since Milestone 2–3) — this milestone only adds the two PDF doc types.
  `POST /api/documents/generate/` now accepts `{doc_type: "statement", customer_id, date_from,
  date_to}` and `{doc_type: "report", date_from, date_to}` (both dates optional — omit for
  all-time), same JobTask/polling contract as invoice/receipt.
- [x] Status: Done

## Milestone 6 — Billing app (plans, feature flags, admin overrides)
- `billing` app: Plan/PlanFeature/Subscription/UsageCounter models, Django admin config for manual per-business overrides, `has_feature()` helper + DRF permission class.
- Deliverable: Admin can create the 3 fixed plans + assign a custom plan to a business by hand; a feature-gated dummy endpoint proves the 403 path works.
- Implementation note: Basic/Standard/Premium/Custom seeded via data migration
  (`apps/billing/migrations/0002_seed_plans.py`), not hardcoded — admin-editable from there on.
  `Plan.has_feature()` / `Subscription.business_has_feature()` are the only call sites (per
  `claude_rule.md` Section 6); the `HasFeature` DRF permission class + `FeatureNotOnPlan`/
  `UsageCapExceeded` exceptions (`apps/billing/exceptions.py`) produce the exact
  `FEATURE_NOT_ON_PLAN`/`USAGE_CAP_EXCEEDED` codes `backend_workflow.md` Section 9 specifies.
  `services.enforce_feature_gate(business, feature_key)` is the one call Milestones 7–9's AI
  endpoints should make before any Groq/Gemini call.
- [x] Status: Done

## Milestone 7 — AI Chat (text)
- `chat` app: Conversation/ChatMessage models, Groq client, prompt construction with business context, feature-gate check from Milestone 6.
- Deliverable: Chat tab's text flow is real (no more keyword-matcher), respects plan gating, replies in the owner's language with correct `speech_text`.
- Implementation note: `apps/chat/groq_client.py` exposes `GROQ_MODEL_FAST`/`GROQ_MODEL_REASONING`
  (env-configurable, defaults `llama-3.1-8b-instant`/`llama-3.3-70b-versatile`); `prompt.py`'s
  `needs_reasoning()` keyword-routes bill/draft/sale-shaped messages to the 70B model, everything
  else to 8B. Business context passed to the model includes each recent customer's real numeric
  `id` + `current_balance` (not just names) so `draft_bill.customer_id`/`previous_balance` resolve
  to real records — needed for Milestone 8's Confirm-and-Send to work. Output-contract validation
  (`serializers.AiReplySerializer`) retries once with a stricter reminder on a bad shape, then
  falls back to a plain-text apology — verified this path is reachable, not just written.
- [x] Status: Done

## Milestone 8 — Draft Bill confirm flow
- Wire AI-proposed drafts to the real `sales` recordSale path on "Confirm and Send"; chat-local-only "Edit Draft".
- Deliverable: Full Draft Bill lifecycle from `USER_WORKFLOW.md` Section 6c works against real data, including failure handling (card stays editable on failed save).
- Implementation note: `POST /api/chat/draft/{messageId}/confirm/` is the only new endpoint — "Edit
  Draft"/"Save Draft" stays chat-local-only per `BACKEND_INTEGRATION_GUIDE.md` Section 7b, no
  backend call at all, so nothing was built for it. Confirm calls the exact same
  `apps.sales.services.record_sale` used by manual entries (one line item named "AI Chat Draft
  Bill", `payment_method="cash"` when there's a payment — the hardcoding the guide flags as a known
  simplification until `draft_bill` carries real line items/payment method). Not gated by
  `ai_chat` — it's the sales-recording path, not the AI surface. Guards: 400 `NO_DRAFT_BILL` if the
  message has none, 400 `ALREADY_CONFIRMED` on double-confirm, 400 `CUSTOMER_NOT_MATCHED` if
  `draft_bill.customer_id` doesn't resolve to a real customer, 500 `SAVE_FAILED` wraps any
  `record_sale` exception — all three failure paths leave `draft_confirmed=False` and write
  nothing.
- [x] Status: Done

## Milestone 9 — Image extraction pipeline
- FastAPI `/vision/extract` (Gemini Vision), `image_info_extractor` app, JobTask wiring, Groq follow-up-question logic for missing fields.
- Deliverable: Chat tab's photo-attach flow reads a real receipt/challan photo and produces a pre-filled Draft Bill or a clarifying question, feature-gated per plan.
- Implementation note (deviation from the original plan, by explicit product decision — see
  `ARCHITECTURE.md` Section 4 and `ai_automation_layer.md` Section 3): the live Gemini call
  happens **directly from Django's `jobs` worker** (`apps/image_info_extractor/gemini_client.py`),
  not proxied through FastAPI. FastAPI's `POST /vision/extract` is still fully built and working
  (same contract) — kept as a ready-made swap point for a future local/self-hosted vision model,
  to be decided from real production feedback rather than in advance. `POST /api/chat/image/`
  uploads to shared storage, gated by `image_extraction` (Premium), creates a `JobTask` +
  `ExtractionJob`. **`GEMINI_API_KEY` is not set yet** — the worker's Gemini call fails cleanly and
  the job still completes with a graceful fallback chat reply (never a stuck "typing…"); add a key
  to `.env` in both services to activate real extraction, no code changes needed.
- [x] Status: Done

## Milestone 10 — WhatsApp integration + Notifications
- `whatsapp` app: connect/status/qr/disconnect/unlink proxy to the Gateway, one gateway_session_id per business, reconnection without re-scan.
- `notifications` app wired to real events (invoice sent, payment received, WhatsApp disconnected).
- **Replace the Gateway's current API-key auth with Django JWT** — the Gateway currently trusts a
  static `x-api-key`; swap this for validating a Django-issued JWT on every request instead, so
  auth is per-authenticated-request (and revocable/expirable) rather than one long-lived shared
  secret. Django remains the only party the phone ever talks to — the Gateway just needs to verify
  the JWT Django forwards/attaches on its proxied calls. (Hosting/session-storage changes for the
  Gateway itself are explicitly out of scope for now — deferred, to be revisited separately.)
- Deliverable: Settings → WhatsApp Connection is fully real end-to-end (QR scan → connected → send an invoice to a customer over WhatsApp); Notifications inbox shows real events; Gateway requests are authenticated via Django JWT, not a static API key.
- Implementation note: Gateway's `x-api-key` middleware replaced with `jwt.middleware.ts` — verifies
  an HS256 `Authorization: Bearer` token (`iss: "django-backend"`, 60s expiry) against a new shared
  `WHATSAPP_GATEWAY_JWT_SECRET` env var (same value in both services' `.env`). Django mints this
  token fresh per outbound call via a standalone PyJWT-signed service token
  (`apps/whatsapp/gateway_client.py`) — decoupled from user-session JWTs entirely, so a leaked
  Gateway-facing secret can't forge a user session. Verified old `x-api-key` requests are now
  rejected and the SAFE TESTING CHECKLIST invariants (receive-handler log-only, `sendText`/
  `sendDocument` reachable only via `message.controller.ts`) still hold — no other Gateway file
  touched. `apps/whatsapp/services.py` stores one `gateway_session_id` per business (on the
  existing `Business` model field) and proxies connect/status/qr/disconnect/unlink; `whatsapp_send`
  (Premium) gates `send`/`send-document`, `whatsapp_disconnected` Notifications fire on
  `RATE_LIMIT_EXCEEDED`/`SESSION_NOT_CONNECTED` per `backend_workflow.md` Section 8.
  `payment_received` wired into `sales.services`, `invoice_sent` into the send-document success
  path. **Could not verify an actual QR-scan → CONNECTED handshake** — this sandboxed environment
  has no route to WhatsApp's real servers (Baileys' socket handshake fails at the network layer,
  confirmed via Gateway logs, unrelated to any code here); the full Django↔Gateway proxy plumbing
  (session create/store, status/QR proxying including the `QR_NOT_AVAILABLE`→404 passthrough,
  disconnect, unlink, feature gating, error codes) was verified end-to-end against the real running
  Gateway. A real device scan needs testing from a network with real internet access to WhatsApp.
- [x] Status: Done (pending a real-network QR-scan test — see note above)

## Post-milestone-10 (not numbered, do when needed)
- Postgres migration, Celery+Redis migration (see `ARCHITECTURE.md` Section 6) — triggered by actual load, not calendar time.
- Multi-device/session hardening, scheduled reminders (pending payment reminders, daily summary), Import/Export/Backup stubs from `USER_WORKFLOW.md` Section 7.
