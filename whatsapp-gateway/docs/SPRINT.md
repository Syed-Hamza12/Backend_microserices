# Sprint & Milestones

## Milestones (5, down from 10 — merged the ones that were really one task split in two)

- [x] **1. Foundation** — TS + Express setup, folder structure, logger, `/health`
- [x] **2. Sessions + Connection** — create/load/restore sessions, Baileys socket, QR login, status endpoint (CONNECTED-status/real-device flow UNVERIFIED — no live QR scan done yet)
- [x] **3. Messaging** — send text, send PDF/document, receive text (log only for now), validation (validation + rate-limiter VERIFIED; sendText/sendDocument reaching real WhatsApp remains UNVERIFIED — no test number available yet)
- [x] **4. Lifecycle + Security** — disconnect, reconnect, unlink, restart recovery, API key middleware
- [ ] **5. Production Ready** — Docker, graceful shutdown, final security pass

Don't start the next milestone until the current one works end-to-end and you've said "go".

---

## Current Sprint

**Milestone:** 4 — Lifecycle + Security
**Status:** DONE (except the carried-forward live-device verification and the known send-queue gap noted below)

**Done (Milestone 1 — Foundation)**
- Documentation simplified ✅
- TypeScript + Express setup ✅
- Folder structure (routes/controllers/services/baileys/middleware/utils/config) ✅
- Logger — using `pino` (already installed, production-standard, chosen over console.log/Winston) ✅
- `.env` + `.env.example` ✅
- `/health` endpoint — verified 200 OK at `/api/v1/health` ✅
- `.gitignore` added ✅

**Done (Milestone 2 — Sessions + Connection)**
- `POST /sessions`, `POST /sessions/:id/connect`, `GET /sessions/:id/qr`, `GET /sessions/:id/status` — implemented, Routes → Controllers → Services → Baileys ✅
- `src/services/session.service.ts` — in-memory session map + socket map, `createSession`, `connectSession`, `getStatus`, `getQr`, `restoreSessionsFromDisk` (per-session try/catch, sets ERROR on failure, never blocks other sessions) ✅
- `src/baileys/socket.factory.ts` + `connection.handler.ts` — Baileys socket creation, QR/connecting/open/close/loggedOut event handling ✅
- Verified with a real Baileys socket: session create → connect → status flips to `QR_READY` → `/qr` returns a real, scannable PNG QR code ✅
- **UNVERIFIED**: the `CONNECTED` status transition and phone-number capture have NOT been tested against a real WhatsApp account. Live QR scan test deliberately deferred until Milestone 3 (Messaging) is built, so a real number is never connected before send-safety limits exist. Do not mark Milestone 2 fully done until this live test happens.

**Done (Milestone 3 — Messaging)**
- Added new CLAUDE_RULES.md safety rule: no automatic/timer/loop/reconnect/auto-reply sends, ever — every send must be an explicit human/API-triggered call ✅
- `src/config/rateLimit.ts` — `MAX_MESSAGES_PER_MINUTE = 5` (deliberately low, pre-launch) ✅
- `src/services/rateLimiter.service.ts` — sliding-window per-session cap, enforced inside `message.service.ts` before every send ✅
- `src/services/message.service.ts` — `sendText` and `sendDocument`, fully separate functions, no shared auto-send pathway ✅
- `sendDocument` — validates https + `.pdf` filename (zod), then fetches and checks: response ok, size ≤15MB (both header and actual bytes), and **magic-bytes `%PDF-` check** (catches a URL that lies about its extension) — all before ever touching the socket ✅
- `src/baileys/message.receiver.ts` — `messages.upsert` listener that only logs; does not import `message.service.ts`, structurally cannot send/reply/forward ✅
- `POST /messages`, `POST /messages/document` wired Routes → Controllers → Services → Baileys ✅
- Error handler now returns proper `400 VALIDATION_ERROR` for zod failures (previously fell through to a generic 500) ✅
- **Verified**: invalid phone number → 400; non-https `fileUrl` → 400; a URL serving HTML but named `fake.pdf` → 422 `DOCUMENT_NOT_PDF` (magic-bytes check works); rate limiter allows 5 sends and blocks the 6th (tested directly against `rateLimiter.service.ts`, throwaway script deleted after) ✅
- **UNVERIFIED**: whether `socket.sendMessage(...)` actually reaches WhatsApp. No test/throwaway WhatsApp number is available yet, and per the new safety rule this will not be tested against the real account until Milestone 4 lifecycle work is further along and a live QR scan + single deliberate send is done together.

**Done (Milestone 4 — Lifecycle + Security)**
- `POST /sessions/:id/disconnect` — closes the Baileys socket (`socket.end()`), keeps `sessions/<id>/creds.json` on disk, status → `DISCONNECTED` ✅
- `DELETE /sessions/:id` — full unlink: `socket.logout()`, deletes `sessions/<id>/` recursively from disk, removes the session from both in-memory maps (`sessions`, `sockets`), status `UNLINKED` returned in the response before the record is deleted ✅
- Reconnect: kept the automatic reconnect-on-unexpected-close logic built in Milestone 2 (`DisconnectReason`-based, in `connection.handler.ts` + the `onClose` hook) for genuine drops; manual reconnect reuses the existing `POST /sessions/:id/connect` endpoint on an already-created session — no duplicate endpoint added ✅
- Race-condition handling in `session.service.ts`: `pendingOps: Set<string>` blocks a second concurrent disconnect/unlink on the same session (`409 OPERATION_IN_PROGRESS`); `intentionalCloses: Set<string>` suppresses the auto-reconnect/onLoggedOut logic when the close was caused by our own disconnect/unlink call, so a manual disconnect is never silently undone ✅
- **Known gap, deliberately scoped out**: no send-queue/lock exists for a message send that's mid-flight when a disconnect happens concurrently — it will either complete against the torn-down socket (surfacing as a normal 500) or the caller's next `getSocket()` call will correctly see `409 SESSION_NOT_CONNECTED`. Fine at current volume; **must be revisited before this handles real traffic at volume** (flagging per your instruction, not silently dropping it).
- `src/middleware/apiKey.middleware.ts` — `requireApiKey`, checks `x-api-key` against `env.API_KEY`, mounted after `healthRoutes` and before `sessionRoutes`/`messageRoutes` so only `/health` is exempt ✅
- `API_KEY` added to `src/config/env.ts` (required, no default) and to `.env.example` as `API_KEY=change-me`. **`.env` itself was not touched** (per CLAUDE_RULES.md) — you need to add your own `API_KEY=...` value there before the server will boot.
- **Verified, actually run (not just reasoned through)**:
  - `/health` returns 200 with no `x-api-key` header; `POST /sessions` returns `401 UNAUTHORIZED` (standard error shape) with a missing key and with a wrong key ✅
  - Restart-recovery check 1: created a session, connected it (status `QR_READY`, folder on disk), called `/disconnect` (status → `DISCONNECTED`, folder untouched, no auto-reconnect fired), restarted the server, confirmed `restoreSessionsFromDisk()` picked it back up and reconnected it (status back to `QR_READY`) ✅
  - Restart-recovery check 2: created a separate session, connected it, called `DELETE` (status `UNLINKED` in response, folder deleted, in-memory record gone, immediate re-`GET /status` → `404`), restarted the server, confirmed no trace whatsoever — no folder, no record, still `404` ✅

**Next up**
- Milestone 5: Production Ready (Docker, graceful shutdown, final security pass) — plan pending approval.
- **UNVERIFIED (carried forward from Milestones 2 & 3)**: the `CONNECTED` status transition against a real WhatsApp account, and whether `sendText`/`sendDocument` actually reach WhatsApp — still no test number available. Live QR scan + one deliberate test send should happen before or during Milestone 5.
- **Known limitation to revisit before real traffic at volume**: no send-in-flight lock/queue around disconnect (see Milestone 4 notes above).

---

## Changelog

Add one line per finished item. Newest on top.

```
[unreleased]
- Milestone 4 (Lifecycle + Security) complete: POST /sessions/:id/disconnect (closes socket, keeps creds on disk), DELETE /sessions/:id (logout + full disk/memory cleanup), API key middleware on all routes except /health. Added pendingOps/intentionalCloses guards in session.service.ts to prevent disconnect/unlink races and to stop manual disconnects from being silently auto-reconnected. Verified by actually running: 401 on missing/wrong API key, 200 on /health without a key, disconnect-then-restart reconnects, unlink-then-restart leaves no trace. Known gap logged: no send-in-flight lock/queue around disconnect — acceptable at current volume, must revisit before real traffic at volume.
- Milestone 3 (Messaging) code-complete: sendText/sendDocument as separate service functions, 5/minute rate limiter enforced pre-send, sendDocument validates https+pdf-extension (zod) then size cap (15MB) then PDF magic-bytes check before touching Baileys, log-only messages.upsert receiver (cannot send/reply), errorHandler now returns 400 for zod validation errors. Added CLAUDE_RULES.md rule banning any automatic/loop/reconnect/auto-reply sends. Verified: bad-input rejection (phone/URL/fake-PDF) and rate-limiter cutoff at 6th request. Real-Baileys-send to an actual WhatsApp number left UNVERIFIED, deliberately deferred until a test number is available.
- Milestone 2 (Sessions + Connection) code-complete: session create/connect/status/qr endpoints, Baileys socket factory + connection handler, restore-on-boot with per-session error isolation. Verified QR generation with a real Baileys socket; CONNECTED-status/real-device flow deliberately left UNVERIFIED pending live scan test after Milestone 3 send-safety limits are in place. Deleted old src/managers/ (SessionManager.ts, GatewaySession.ts, sessionStatus.ts) and stray src/test.ts.
- Milestone 1 (Foundation) complete: TS+Express setup, folder structure, pino logger, .env/.env.example, .gitignore, /health endpoint verified working
```