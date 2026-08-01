# Backend Architecture

## 1. System overview

```
Flutter Mobile App
      │  HTTPS (JWT)
      ▼
Django Backend (REST API)  ── the only thing the phone ever talks to
      │
      ├──► SQLite (dev/now) ──► Postgres (future, see Scaling)
      │
      ├──► DB-backed Job Queue (JobTask table) ─┬─► FastAPI Microservice
      │                                          │      ├─ PDF generation (invoice/statement/receipt/report)
      │                                          │      └─ Image extraction (Gemini Vision → structured JSON)
      │
      ├──► AI Chat Engine (in Django, `apps/chat`) ──► Groq API (8B for simple turns, 70B for reasoning/draft-bill — see ai_automation_layer.md)
      │
      └──► WhatsApp app (`apps/whatsapp`, thin client) ──► WhatsApp Gateway microservice (Node/Baileys) ──► Customer's WhatsApp
```

**Why this shape:**
- The phone never sees the Groq key, Gemini key, or WhatsApp Gateway key/URL — only Django does. Matches the existing Flutter integration guide's "three hard rules."
- PDF generation and image extraction are CPU/latency-heavy and best kept out of the main Django process — a stuck OCR call must never slow down a customer list load. Isolating them in FastAPI + a queue means Django's request/response cycle stays fast.
- Everything is designed so **today's DB-backed queue and SQLite can be swapped for Celery+Redis and Postgres later without changing any app-level API contracts** — see Section 6 (Scaling Path).

## 2. Repos / folders

```
Backend folder/
├── django-backend/            # apps/, config/, manage.py, requirements.txt, .env
├── fastapi-service/           # pdf/, image_extraction/, main.py, requirements.txt, .env
├── whatsapp-gateway/          # existing Node/Baileys service (already built, per Postman doc)
└── docs/                      # this folder — all architecture/process docs for Claude Code CLI
```

Each service has its own `.env`, its own `requirements.txt`/`package.json`, and is independently
deployable/restartable. Django is the only service exposed to the internet at large; FastAPI and
the WhatsApp Gateway are internal-only (localhost / private network / firewalled).

## 3. Django apps

| App | Responsibility |
|---|---|
| `accounts` | Auth (Google OAuth + email), Business Profile, Plan/Subscription, feature flags |
| `customers` | Customer CRUD, balances, notes |
| `sales` | Sales, payments, adjustments, activity/history, balance cascade recalculation |
| `documents` | Orchestrates PDF jobs (invoice/statement/receipt/report) — talks to FastAPI, not a PDF library itself |
| `chat` | AI chat text pipeline — builds prompt/context, calls Groq, stores conversation + messages |
| `image_info_extractor` | Orchestrates image jobs (receipt/challan photos) — talks to FastAPI's vision endpoint, runs the Groq "ask for missing field" follow-up logic |
| `whatsapp` | Thin wrapper around the Gateway's REST API (create/connect/status/qr/send/disconnect/unlink) — holds the Gateway's `x-api-key` |
| `notifications` | Notification records (invoice sent, payment received, WhatsApp disconnected, etc.) |
| `billing` | Plans, feature flags, manual admin overrides, payment record-keeping (see `business_logic.md`) |
| `jobs` | Generic `JobTask` model + dispatcher used by `documents` and `image_info_extractor` (see Section 5) |

## 4. FastAPI microservice

Two responsibilities, each its own router:

- `POST /pdf/generate` — body: `{doc_type, business_id, payload}` → returns a PDF file (or writes to shared storage and returns a path/URL). `doc_type` ∈ `invoice | statement | receipt | report`. This one **is** in Django's live request path — the `jobs` worker calls it for every generated document.
- `POST /vision/extract` — body: multipart image + `business_id` → calls Gemini Vision, normalizes the response into `{date, amount, customer_name, line_items[], raw_text}`, returns JSON. **As of Milestone 9, Django's `jobs` worker does not call this** — it calls Gemini directly from `apps/image_info_extractor/gemini_client.py` instead (see `ai_automation_layer.md` Section 3 for why). This endpoint is still fully implemented and kept live so it's a ready-made swap point: point the worker at it instead of calling Gemini directly the day a self-hosted/local vision model replaces Gemini, without touching Django's `image_info_extractor` business logic (customer matching, clarification-question generation) at all. Decide which path is "live" based on real production feedback, not in advance.

FastAPI is stateless — no database of its own. It reads only what Django sends it in the request
and returns only the result. This keeps it trivially horizontally-scalable later (Section 6).

Auth between Django and FastAPI: a shared internal API key header (`X-Internal-Key`), never
exposed to the phone, rotated independently of the Gateway's key.

## 5. Job queue (current: DB-backed, future: Celery+Redis)

For now (target: up to ~1000 users), a simple `JobTask` table in Django is the queue:

```
JobTask
  id, type ("pdf" | "image_extract"), business_id, status ("queued"|"processing"|"done"|"failed"),
  payload (JSON), result (JSON, nullable), error (text, nullable),
  created_at, started_at, finished_at
```

- A lightweight worker loop (management command `runworker`, run via `systemd`/`supervisor`, or
  Django-Q/`django-background-tasks` if you want built-in scheduling instead of hand-rolling the
  loop) picks the oldest `queued` row, marks it `processing`, calls the FastAPI endpoint
  synchronously, writes the result back, marks `done`/`failed`.
- **This gives you the load-balancing behavior you asked for "for free":** one worker process
  naturally processes one PDF/image job at a time, queuing the rest — no separate infra needed at
  1000-user scale. Run 2–3 worker processes if you want mild parallelism without adding Redis yet.
- Django exposes `GET /api/jobs/{id}/` so the phone can poll job status (same polling pattern
  already used for WhatsApp QR/status in the existing Gateway integration).

**This table's contract is intentionally the same shape a Celery task record would have**
(`status`, `payload`, `result`, `error`) so that swapping the worker loop for real Celery+Redis
later is a drop-in change: same `JobTask` writes, just enqueued via `.delay()` instead of a DB
poll loop. No API consumer (Flutter, or `documents`/`image_info_extractor` app code) needs to
change.

## 6. Scaling path (SQLite → Postgres, DB-queue → Celery+Redis)

Not needed now — documented so future-you doesn't have to redesign anything, just swap:

| Today (≤1000 users) | Future (when it's actually needed) |
|---|---|
| SQLite, single file | Postgres — Django's ORM makes this a settings change + `manage.py dumpdata`/`loaddata` or a proper migration; avoid SQLite-only features (no raw `PRAGMA`/vendor-specific SQL in app code) to keep this cheap |
| DB-backed `JobTask` queue, polling worker loop | Celery + Redis — same `JobTask` write pattern, workers become Celery tasks, Redis becomes the broker; `documents`/`image_info_extractor` app code barely changes since they already just "create a JobTask and poll" |
| Single Django process (`gunicorn` + a few workers) | Multiple Django instances behind a load balancer (stateless by design — no server-side session storage beyond the DB) |
| FastAPI single instance | Multiple FastAPI instances behind a load balancer — trivial since it's already stateless |

**Rule of thumb baked into the app-layer design from day one:** nothing in Django's app code
should assume "the queue is synchronous" or "the DB is SQLite" — always go through the `jobs`
app's dispatcher functions and Django's ORM, never raw SQL.

## 7. Security
- JWT (or DRF token) auth between Flutter and Django; refresh-token rotation recommended.
- Gateway `x-api-key`, Groq key, Gemini key, internal FastAPI key: all server-side env vars, never returned in any API response.
- Rate limiting on `chat` and `image_info_extractor` endpoints (both cost real money per call to Groq/Gemini) — see `business_logic.md` for plan-based limits.
- Per-business data isolation enforced at the ORM queryset level (every model FK's back to `Business`; every view filters by `request.user.business`).
