# Document Generation & WhatsApp Delivery

How a bill gets from the database to a customer's phone, and which service owns
what.

## The flow

```
Business data  (Django — source of truth for every figure)
      │
      ├─ build_payload_for()      formats the numbers
      │
      ▼
FastAPI  POST /documents/render   payload -> bytes (PNG or PDF)
      │
      ▼
Gateway  POST /messages/media     bytes uploaded, sent over WhatsApp
      │
      ▼
bytes go out of scope             nothing was ever written to disk
      │
      ▼
DocumentDelivery                  audit row: what, to whom, did it arrive
```

**Nothing is stored.** A generated document exists in memory long enough to be
sent, then it is gone. The database is the permanent record: if a customer
deletes the file, the owner regenerates it from the same data and sends it
again. There is no cleanup job because there is nothing to clean up.

## Responsibilities

| Service | Owns |
|---|---|
| **Django** | Every figure, every format decision, permission and feature gating, the audit trail |
| **FastAPI** | Layout only. Turns a payload into bytes. Knows nothing about the business |
| **Gateway** | Transport only. Sends bytes it is handed. Never fetches anything |
| **Flutter** | Display only. Renders no documents (removed in Phase 4) |

Django formats the *values* too — dates as `30 Jul 2026`, money to two
decimals — because it owns them. Handing the renderer raw values put
`2026-07-30T18:47:44.786536+00:00` and `3000.0000` in front of customers.

## Django API

### `GET /api/documents/formats/`
Which formats each document type supports, and its default. Lets the app offer
only valid choices rather than surfacing an error after Send is tapped.

```json
{"invoice": {"formats": ["image","pdf"], "default": "image"},
 "statement": {"formats": ["pdf"], "default": "pdf"}}
```

### `POST /api/documents/render/`
Renders and **returns the file itself**, for preview and sharing. Synchronous —
someone is waiting to look at it, and rendering is fast. Nothing is stored.

```json
{"doc_type": "invoice", "target_id": 42, "format": "image"}
```

Responds with `image/png` or `application/pdf`. The `X-Document-Format` header
states what was actually produced.

### `POST /api/documents/send/`
Queues a render-and-send. Returns `202` with a `job_id` and the pending
delivery. Requires the `whatsapp_send` feature and a connected WhatsApp session.

```json
{"doc_type": "invoice", "target_id": 42, "format": "image", "to": "923001112222"}
```

`to` is optional — it defaults to the phone number of the customer the document
belongs to.

Runs in the background worker because the Gateway paces sends deliberately to
protect the business's WhatsApp number, which takes several seconds. The target
is validated *before* queueing, so a bad request fails immediately instead of
becoming a failed job the owner has to discover.

### `GET /api/documents/deliveries/` and `/deliveries/<id>/`
Delivery history and single-delivery status, for polling after a send.

## Formats

| Document | Default | Also | Why |
|---|---|---|---|
| Invoice / Bill | **Image** | PDF | Customers read it inline in WhatsApp without downloading |
| Receipt | **Image** | PDF | Same |
| Statement | **PDF** | — | Multi-page by nature |
| Report | **PDF** | — | Multi-page by nature |

Images are sent as WhatsApp **images** (inline), PDFs as **document
attachments**.

### Format substitution

A bill too long to be readable as an image comes back as a PDF. This is never
silent: FastAPI reports it in `X-Document-Format`, Django records it, and
`DocumentDelivery` keeps both `requested_format` and `delivered_format`. The
audit trail shows what the customer actually received.

## The audit record

`DocumentDelivery` replaces the old `GeneratedDocument`, which stored a
`file_url` pointing at a PDF on disk — meaningless once files are transient.

Fields worth knowing:

- `requested_format` / `delivered_format` — see substitution above
- `to_phone` — denormalised on purpose: the number **as dialled**. If the
  customer's number is edited later, this still shows where the document went
- `parameters` — enough to reproduce the document exactly
- `status` — `pending` → `sending` → `sent` / `failed`
- `error_code` / `error_message` — why a failure happened

A delivery can only move out of `pending` once, so a job processed twice can
never send a customer the same document a second time.

## Safety properties

- **No SSRF surface.** The Gateway used to fetch a URL; it now receives bytes.
  It makes no outbound fetch on our behalf at all.
- **No public media URL** is needed for sending, so document delivery works
  over plain HTTP in development.
- **Magic-byte checks.** The Gateway verifies the bytes match the claimed type;
  a `.png` filename with PDF or junk content is refused.
- **Filenames** cannot contain path separators.
- **Session-scoped tokens.** A token minted for one gateway session cannot send
  from another.
- **Autoescape** in the renderer — see `fastapi-service/docs/DOCUMENT_RENDERING.md`.

## Running it

Three services plus the worker:

```bash
# 1. FastAPI (rendering)
cd fastapi-service && ../venv/Scripts/python.exe -m uvicorn main:app --port 8001

# 2. WhatsApp Gateway
cd whatsapp-gateway && npm run dev

# 3. Django
cd accountant_backend && ../venv/Scripts/python.exe manage.py runserver 0.0.0.0:8000

# 4. The worker — sends do not happen without it
cd accountant_backend && ../venv/Scripts/python.exe manage.py runworker
```

Tests:

```bash
cd accountant_backend && ../venv/Scripts/python.exe manage.py test    # 52
cd fastapi-service   && ../venv/Scripts/python.exe -m pytest tests -q  # 20
cd whatsapp-gateway  && npm run typecheck
```

## Removed in this phase

| Removed | Replacement |
|---|---|
| `POST /api/documents/generate/` | `/documents/render/` and `/documents/send/` |
| `POST /api/whatsapp/send-document/<id>/` | `POST /api/documents/send/` |
| `GeneratedDocument` model | `DocumentDelivery` |
| Job type `pdf` | Job type `document_send` |
| Gateway `POST /messages/document` (fileUrl) | `POST /messages/media` (raw bytes) |
| `DOCUMENT_FETCH_ALLOWED_HOSTS` env var | no longer needed — nothing is fetched |

`POST /pdf/generate` on FastAPI is deprecated and unused; it is removed once
nothing references it.
