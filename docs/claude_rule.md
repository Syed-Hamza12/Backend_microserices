# Claude Code CLI — Working Rules for This Project

Read `project_vision.md`, `ARCHITECTURE.md`, and `backend_workflow.md` before writing any code in
a new session. Treat this repo like a new accountant employee would treat a company handbook —
these rules are not suggestions.

## 1. Repo boundaries
- Three independently-runnable services: `django-backend/`, `fastapi-service/`, `whatsapp-gateway/`.
  Never import code across service boundaries — they only talk over HTTP.
- `docs/` is documentation only — never put runnable app code there.
- Don't touch `whatsapp-gateway/` unless explicitly asked — it's already built and tested per its
  own Postman guide; Django only calls its REST API.

## 2. Django conventions
- Every model that belongs to a business has a `business = models.ForeignKey(Business, ...)`.
  Every queryset in every view **must** filter by the authenticated user's business — no
  exceptions, no "I'll add the filter later." This is the multi-tenancy boundary; treat leaking it
  as a security bug, not a style nit.
- No raw SQL, no SQLite-specific syntax — always the ORM. (See `ARCHITECTURE.md` Section 6 —
  Postgres migration must be a config change only.)
- All money fields are `DecimalField`, never `FloatField`.
- All timestamps are timezone-aware (`USE_TZ = True`), stored UTC, converted at the edge.
- New endpoints go under `apps/<domain>/`, one `serializers.py` + `views.py` + `urls.py` per app —
  don't create a monolithic `api/` app.
- Every endpoint that costs money to call downstream (chat, image extraction, PDF gen) must check
  the business's plan/feature-flags (see `business_logic.md`) **before** doing any expensive work,
  not after.

## 3. Jobs / async work
- PDF generation and image extraction are **always** dispatched through the `jobs` app's
  `JobTask` model — never call FastAPI directly from a request/response view. Create the
  `JobTask`, return its id immediately, let the worker loop process it. See `ARCHITECTURE.md`
  Section 5.
- Don't hand-roll a second queue mechanism "just for this one feature." One `JobTask` table, one
  worker loop, for everything until the Celery+Redis migration.

## 4. FastAPI conventions
- Stateless. No database. If you think FastAPI needs to remember something between requests,
  that's a sign the thing belongs in Django instead.
- Every endpoint requires the `X-Internal-Key` header — reject anything without it. This service
  is never exposed to the public internet.
- Keep PDF templates and vision-prompt templates in clearly named files (`templates/invoice.html`,
  `prompts/receipt_extraction.txt`) — don't inline large template strings in Python.

## 5. AI / LLM conventions
- All Groq calls go through one helper (`apps/chat/groq_client.py` or similar) — one place to
  change model name, retry logic, and timeout, not scattered `requests.post` calls.
- Every AI response that could become a real database write (a Draft Bill, a customer match) must
  go through the existing **Confirm and Send** pattern already established by the Flutter app —
  AI never writes to `sales`/`payments` directly; it only produces a draft the owner explicitly
  confirms. Do not "helpfully" auto-save AI-extracted data.
- Prompts must explicitly instruct the model to answer in the business owner's chosen language
  (English/Urdu/Roman Urdu) and to also return a native-Urdu-script `speech_text` when the display
  language is Roman Urdu (on-device TTS mispronounces Latin-script Roman Urdu — already discovered
  and documented in the Flutter integration guide).

## 6. Billing / feature gating
- Never hardcode a tier check like `if plan == "800"` in view logic — always check a named
  feature flag (`business.plan.has_feature("ai_chat")`) so admin-editable custom plans work
  without new code. See `business_logic.md`.
- A business with AI features disabled must still get full, working manual bookkeeping — never
  degrade core CRUD behind a paywall, only the AI/automation surface.

## 7. Testing / safety before touching WhatsApp
- Follow the existing Postman "SAFE TESTING CHECKLIST" in the Gateway's own docs before wiring any
  new Django code path that can call `sendText`/`sendDocument`. Never add a second code path that
  can trigger a WhatsApp send outside `apps/whatsapp`.

## 8. When something is ambiguous
- Prefer the simplest thing that fits `ARCHITECTURE.md`'s stated shape over inventing a new
  pattern. If a decision would lock in something hard to reverse (a new service, a new datastore,
  a schema that's awkward to migrate to Postgres), stop and ask rather than guessing.
- Update the relevant doc in `docs/` in the same PR/commit as any change that makes it stale —
  these docs are meant to stay accurate, not be a one-time snapshot.
