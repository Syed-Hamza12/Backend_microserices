# Architecture

## 3-Layer Flow

Simplified from the usual 5-layer setup — at 1000 users, one extra layer just means one more file to open for every change.

```
Routes  →  Controllers  →  Services  →  Baileys
```

- **Routes** — define endpoints, attach middleware, forward to controller. No logic.
- **Controllers** — validate the request shape, call a service, send the HTTP response. Thin.
- **Services** — everything else: session creation/lookup/restore, sending messages, talking to Baileys. This is where "Session Manager" logic from the old docs now lives — it's not a separate layer, just a service file (`session.service.ts`).
- **Baileys** — only ever called from inside services, never directly from a controller.

```
❌ Route → Baileys
✔ Route → Controller → Service → Baileys
```

## Folder Structure

```
whatsapp-gateway/
├── src/
│   ├── server.ts
│   ├── config/          # env vars, constants
│   ├── routes/
│   ├── controllers/
│   ├── services/
│   │   ├── session.service.ts   # create/load/restore/delete sessions
│   │   └── message.service.ts   # send/receive messages
│   ├── baileys/         # socket creation, QR, connection events
│   ├── middleware/      # api-key check, error handler
│   ├── types/           # SessionStatus enum, shared types
│   └── utils/           # logger, response helper, uuid
├── sessions/            # creds.json per gatewaySessionId — gitignored
├── logs/
├── .env
└── package.json
```

That's it. No `interfaces/`, `validators/`, `events/`, `constants/` as separate folders — fold small stuff into `types/` and `utils/` until a folder actually earns its place (i.e. has 4+ files in it).

## Naming (keep it simple, keep it consistent)

- Files: `session.service.ts`, `message.controller.ts`
- Classes: `PascalCase`
- Functions/variables: `camelCase`
- Constants: `UPPER_SNAKE_CASE`

## API Endpoints

Base URL: `http://localhost:3000/api/v1`

Auth header on every protected route: `x-api-key: YOUR_KEY`

**Standard success response**
```json
{ "success": true, "data": {} }
```

**Standard error response**
```json
{ "success": false, "error": { "code": "SESSION_NOT_FOUND", "message": "Gateway session not found." } }
```

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/health` | Is the gateway alive |
| POST | `/sessions` | Create a new gateway session |
| POST | `/sessions/:id/connect` | Start WhatsApp connection, triggers QR |
| GET | `/sessions/:id/qr` | Get current QR code (image/png) |
| GET | `/sessions/:id/status` | Get session status |
| POST | `/messages` | Send a text message |
| POST | `/messages/document` | Send a PDF/document |
| POST | `/sessions/:id/disconnect` | Close socket, keep session files |
| DELETE | `/sessions/:id` | Logout + delete session permanently |

`POST /messages` body:
```json
{ "gatewaySessionId": "uuid", "to": "923001234567", "message": "Hello" }
```

`POST /messages/document` body:
```json
{ "gatewaySessionId": "uuid", "to": "923001234567", "fileUrl": "https://.../invoice.pdf", "fileName": "invoice.pdf" }
```

Use a `fileUrl` (gateway fetches and forwards the PDF) instead of multipart upload — keeps the endpoint simple and avoids you needing multer/file-handling middleware. Validate: `fileUrl` is https, `fileName` ends in `.pdf`, and cap fetched file size (e.g. reject over 15MB) before sending to Baileys.

Service split stays the same — add `sendDocument()` next to `sendText()` in `message.service.ts`. No new layer needed.

## Security — the essentials only

You don't need the full checklist from the old doc yet. These four things matter at 1000 users:

1. **API key on every protected route** — reject with 401 if missing/wrong.
2. **Validate every request body** — required fields, types, no empty strings. Use `zod` (simple, works great with Express/TS, you'll like it coming from Django's form validation).
3. **Never log** API keys, `creds.json` contents, or session credentials.
4. **`.gitignore`**: `.env`, `sessions/`, `logs/`, `node_modules/`.

That's the whole list. Rate limiting, JWT, Docker secrets — add later, when you actually have a reason to (e.g. you see abuse in logs, or you move to multiple servers).

## When You Actually Need to Scale Past This

Only revisit this doc when one of these becomes true:
- Single server can't handle the session count → then look at Postgres for session metadata.
- You need multiple gateway instances → then look at Redis for shared state.
- Not before. Scaling early is wasted effort you'll have to redo anyway.