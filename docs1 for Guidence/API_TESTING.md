# Manual API Testing Guide (Postman)

Practical, step-by-step. For the formal endpoint spec see `ARCHITECTURE.md` — this doc is "what to actually click in Postman, in what order, and what to watch for."

## Before you start

- Run the server with logs visible in your terminal (`npm run dev`) — you'll want to watch them live during the QR-scan test.
- Make sure `.env` has a real `API_KEY` set (not the `.env.example` placeholder).

## Base URL and auth

```
Base URL:  http://localhost:3000/api/v1
```

Every endpoint **except `/health`** requires this header:

```
x-api-key: <the value of API_KEY in your .env>
```

Missing or wrong key → `401 Unauthorized`:
```json
{ "success": false, "error": { "code": "UNAUTHORIZED", "message": "Missing or invalid API key." } }
```

---

## SAFE TESTING CHECKLIST — do this before scanning with your real number

- [ ] Rate limiter is active: `src/config/rateLimit.ts` has `MAX_MESSAGES_PER_MINUTE = 5`
- [ ] Receive handler is log-only: open `src/baileys/message.receiver.ts` and re-read its import list — it must **not** import `message.service.ts` (only `@whiskeysockets/baileys` types and the logger)
- [ ] No other code path calls `sendText`/`sendDocument` except `message.controller.ts` → `message.routes.ts` (`grep -rn "sendText\|sendDocument" src/` should only show `message.service.ts`, `message.controller.ts`, and `message.routes.ts`)
- [ ] Server logs are visible in your terminal (`npm run dev`, not backgrounded silently) so you can watch each event as it happens during the scan

If any of these don't check out, stop and fix them before connecting a real number.

---

## Full lifecycle test, in order

### 1. Health check

```
GET http://localhost:3000/api/v1/health
```
No headers required.

**Success (200):**
```json
{ "success": true, "message": "Gateway is healthy.", "data": {} }
```

**Watch for:** if this fails, nothing else will work — check the server is actually running.

---

### 2. Create a session

```
POST http://localhost:3000/api/v1/sessions
```
**Headers:** `Content-Type: application/json`, `x-api-key: <your key>`

**Body:**
```json
{ "displayName": "My WhatsApp Business" }
```

**Success (201):**
```json
{
  "success": true,
  "message": "Session created.",
  "data": {
    "id": "7d44aa24-54c2-42d3-a5f6-44c6a228e365",
    "displayName": "My WhatsApp Business",
    "status": "CREATED",
    "createdAt": "2026-07-25T09:46:07.700Z",
    "updatedAt": "2026-07-25T09:46:07.700Z"
  }
}
```

**Error (400) — missing displayName:**
```json
{ "success": false, "error": { "code": "VALIDATION_ERROR", "message": "displayName: Invalid input: expected string, received undefined" } }
```

**Watch for:** copy the `id` from the response — you'll need it (as `gatewaySessionId` / `:id`) for every step below.

---

### 3. Connect (start the WhatsApp connection)

```
POST http://localhost:3000/api/v1/sessions/{id}/connect
```
**Headers:** `x-api-key: <your key>`

**Success (200):**
```json
{ "success": true, "message": "Connection started. Poll /qr or /status for progress.", "data": { "id": "7d44aa24-54c2-42d3-a5f6-44c6a228e365" } }
```

**Error (404) — bad id:**
```json
{ "success": false, "error": { "code": "SESSION_NOT_FOUND", "message": "Gateway session not found." } }
```

**Watch for:** this returns immediately — it doesn't wait for the QR to be ready. The socket is created in the background. Give it a second or two, then move to step 4.

---

### 4. Get the QR code

```
GET http://localhost:3000/api/v1/sessions/{id}/qr
```
**Headers:** `x-api-key: <your key>`

**Success (200):** raw PNG image (`Content-Type: image/png`) — in Postman, use "Send and Download" or view it in the Preview tab.

**Error (404) — QR not ready yet:**
```json
{ "success": false, "error": { "code": "QR_NOT_AVAILABLE", "message": "No QR code available for this session right now." } }
```

**Watch for:** if you get `QR_NOT_AVAILABLE`, the status probably isn't `QR_READY` yet — go check step 5 first and poll until it is. QR codes also expire after ~60 seconds; if you wait too long between fetching and scanning, re-fetch this endpoint for a fresh one.

---

### 5. Poll status until CONNECTED

```
GET http://localhost:3000/api/v1/sessions/{id}/status
```
**Headers:** `x-api-key: <your key>`

**Success (200), before scanning:**
```json
{
  "success": true,
  "message": "Success",
  "data": {
    "id": "7d44aa24-54c2-42d3-a5f6-44c6a228e365",
    "displayName": "My WhatsApp Business",
    "status": "QR_READY",
    "createdAt": "...",
    "updatedAt": "...",
    "qr": "2@...long base64-ish string..."
  }
}
```

**Now scan the QR** (WhatsApp app → Linked Devices → Link a Device) with your real phone.

Keep polling this endpoint every couple seconds. Status should move:
`QR_READY` → `CONNECTING` → `CONNECTED`

**Success (200), after scanning:**
```json
{
  "success": true,
  "message": "Success",
  "data": {
    "id": "7d44aa24-54c2-42d3-a5f6-44c6a228e365",
    "displayName": "My WhatsApp Business",
    "status": "CONNECTED",
    "phone": "923001234567",
    "createdAt": "...",
    "updatedAt": "...",
    "connectedAt": "..."
  }
}
```

**Watch for:** this is the step that has never been tested against a real device before — watch your terminal logs closely here. If status goes to `ERROR` instead of `CONNECTED`, check the logs for the actual Baileys error before retrying. If it just seems stuck on `CONNECTING`, give it a bit longer — the first connection does a data sync.

---

### 6. Send a text message

```
POST http://localhost:3000/api/v1/messages
```
**Headers:** `Content-Type: application/json`, `x-api-key: <your key>`

**Body:**
```json
{
  "gatewaySessionId": "7d44aa24-54c2-42d3-a5f6-44c6a228e365",
  "to": "923001234567",
  "message": "Test message from the gateway."
}
```
`to` is digits only, international format, no `+` or spaces.

**Success (200):**
```json
{ "success": true, "message": "Message sent.", "data": {} }
```

**Error (409) — session not connected:**
```json
{ "success": false, "error": { "code": "SESSION_NOT_CONNECTED", "message": "Gateway session is not connected." } }
```

**Error (429) — rate limit hit:**
```json
{ "success": false, "error": { "code": "RATE_LIMIT_EXCEEDED", "message": "Send limit of 5 messages per minute exceeded for this session." } }
```

**Watch for:** send this **to your own number** (or a second device/number you control) first, one at a time — do not batch-send. Remember the cap is 5/minute shared between text and document sends on this session.

---

### 7. Send a document (PDF)

```
POST http://localhost:3000/api/v1/messages/document
```
**Headers:** `Content-Type: application/json`, `x-api-key: <your key>`

**Body:**
```json
{
  "gatewaySessionId": "7d44aa24-54c2-42d3-a5f6-44c6a228e365",
  "to": "923001234567",
  "fileUrl": "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf",
  "fileName": "test.pdf"
}
```

**Success (200):**
```json
{ "success": true, "message": "Document sent.", "data": {} }
```

**Error (422) — not actually a PDF:**
```json
{ "success": false, "error": { "code": "DOCUMENT_NOT_PDF", "message": "Fetched file is not a valid PDF (magic bytes mismatch)." } }
```

**Error (422) — too large:**
```json
{ "success": false, "error": { "code": "DOCUMENT_TOO_LARGE", "message": "Document exceeds the 15MB size limit." } }
```

**Watch for:** `fileUrl` must be `https://` and `fileName` must end in `.pdf`, but the server also fetches the file and checks the actual bytes (`%PDF-` header) — a URL that lies about being a PDF will be rejected even if the extension looks right. This counts against the same 5/minute cap as text sends.

---

### 8. Disconnect

```
POST http://localhost:3000/api/v1/sessions/{id}/disconnect
```
**Headers:** `x-api-key: <your key>`

**Success (200):**
```json
{
  "success": true,
  "message": "Session disconnected. Session files kept on disk.",
  "data": { "id": "...", "displayName": "...", "status": "DISCONNECTED", "...": "..." }
}
```

**Watch for:** this closes the socket but keeps `sessions/{id}/` on disk (your linked-device credentials survive). It will **not** auto-reconnect — that's the point.

---

### 9. Reconnect

Same endpoint as step 3 — reused, not a new one:

```
POST http://localhost:3000/api/v1/sessions/{id}/connect
```

**Watch for:** since the creds are still on disk, this should reconnect **without a new QR scan** — poll `/status` and confirm you go straight to `CONNECTED` this time, skipping `QR_READY`. If you instead get a fresh QR, something about the disconnect broke the saved credentials — worth flagging.

---

### 10. Unlink (permanent delete)

```
DELETE http://localhost:3000/api/v1/sessions/{id}
```
**Headers:** `x-api-key: <your key>`

**Success (200):**
```json
{
  "success": true,
  "message": "Session unlinked and deleted.",
  "data": { "id": "...", "displayName": "...", "status": "UNLINKED", "...": "..." }
}
```

**Watch for:** this is destructive and irreversible — it logs the device out of WhatsApp (check your phone's Linked Devices list, it should disappear) and deletes `sessions/{id}/` from disk entirely. Only run this once you're done testing. After this, `GET /status` on the same `id` will return `404 SESSION_NOT_FOUND` — that's expected, not a bug.

---

## Quick reference: error shape

Every error response, regardless of endpoint, follows this shape:
```json
{ "success": false, "error": { "code": "SOME_CODE", "message": "Human-readable explanation." } }
```

Codes you may see: `VALIDATION_ERROR` (400), `UNAUTHORIZED` (401), `SESSION_NOT_FOUND` (404), `QR_NOT_AVAILABLE` (404), `SESSION_NOT_CONNECTED` (409), `OPERATION_IN_PROGRESS` (409), `DOCUMENT_FETCH_FAILED` / `DOCUMENT_TOO_LARGE` / `DOCUMENT_NOT_PDF` (422), `RATE_LIMIT_EXCEEDED` (429), `INTERNAL_SERVER_ERROR` (500), `NOT_FOUND` (404, unknown route).
