# WhatsApp Gateway — Architecture Guidance & Distributed Roadmap

> Audience: a 2nd-year Software Engineering student building a real SaaS. Every new concept below is explained in plain language first (what it is, why it exists, what problem it solves) before any code/schema detail. Nothing here recommends Kubernetes, Redis, Kafka, or Docker Swarm — those are solutions for problems this project does not have yet.

---

## 1. Executive Summary

The WhatsApp Gateway (`whatsapp-gateway/src`) is a single Node.js/Express/Baileys service that Django (`accountant_backend/apps/whatsapp`) talks to over HTTP with short-lived, session-scoped JWTs. Today it runs as **one process, on one machine, storing WhatsApp session credentials as files on that machine's local disk** (`whatsapp-gateway/sessions/<sessionId>/`). This works correctly for one deployment, and the connection-lifecycle code is already unusually careful about the single biggest real risk in this domain — getting WhatsApp numbers banned or rate-limited by reconnecting too aggressively. The gap is scale and resilience: if this one process dies, is redeployed, or needs to run more than one instance (for more WhatsApp numbers than one machine should hold, or for zero-downtime deploys), there is currently no way to do that safely, because "session lives on this disk" and "session is owned by this process" are the same fact with no abstraction between them. This document audits the current code precisely, then lays out a staged, non-breaking path: first make storage swappable (a seam, not a rewrite), then optionally add Firestore as a second storage backend, then introduce a lightweight gateway registry so Django can route sessions to one of several gateway instances — all without touching the reconnect/ban-safety logic that already exists and works.

---

## 2. Current Architecture Review (What Actually Exists Today)

**Topology:** one Django backend (`accountant_backend/`) talks to one WhatsApp Gateway process (`whatsapp-gateway/`) over plain HTTP, using a base URL from a single environment variable:

```python
# accountant_backend/accountant_backend/settings.py
WHATSAPP_GATEWAY_BASE_URL = os.environ.get("WHATSAPP_GATEWAY_BASE_URL", "http://localhost:3000/api/v1")
WHATSAPP_GATEWAY_JWT_SECRET = os.environ.get("WHATSAPP_GATEWAY_JWT_SECRET", "")
```

There is exactly one gateway URL. Nothing in Django today can address "gateway #2" — there is no registry, no round robin, no Firebase. This is the single most important existing-code fact: **any multi-gateway design has to be added, not switched on.**

**Django to Gateway auth:** `accountant_backend/apps/whatsapp/gateway_client.py`'s `_service_token()` mints a 60-second HS256 JWT with `iss="django-backend"`, `aud="whatsapp-gateway"`, and — critically — `sub` set to the one `gateway_session_id` the token is allowed to act on (or `scope="session:create"` for the one endpoint that doesn't have a session id yet). The gateway verifies this in `whatsapp-gateway/src/middleware/jwt.middleware.ts`'s `requireDjangoJwt`, then `assertSessionScope()`/`assertCreateScope()` enforce that a token minted for session A cannot touch session B. This is real tenant isolation, not just "is this Django" — worth preserving in any redesign.

**Django's model of a session:** `accountant_backend/apps/accounts/models.py` has one field, `gateway_session_id = models.CharField(...)`, on the business/account model. `accountant_backend/apps/whatsapp/services.py`'s `_ensure_session_id(business)` lazily calls `gateway_client.create_session()` once per business and stores the returned id forever. Every other operation (`connect`, `get_status`, `send_text`, `disconnect`, `unlink`) is a thin HTTP call keyed by that stored id. Django does not know or care which physical gateway machine holds that session — because today there is only one.

**Session storage on disk:** the gateway stores WhatsApp's cryptographic auth state (identity keys, session keys, app state sync keys) as multiple JSON files per session directory, one directory per session under `SESSION_PATH` (defaults to `./sessions`, `whatsapp-gateway/src/config/env.ts`). As of the Phase 1 refactor (§5, already shipped), nothing in `socket.factory.ts` or `session.service.ts` calls `fs`/`useMultiFileAuthState` directly anymore — both go through a `StorageProvider` interface (`whatsapp-gateway/src/storage/`), and today's only real implementation (`LocalStorageProvider`) writes the exact same files, in the exact same layout, as before. Behavior is unchanged; what changed is that the rest of the app no longer knows *how* those bytes are persisted. `whatsapp-gateway/sessions/` currently only contains `rate-limit-state.json` (the send-rate-limiter's persisted history — see §5) because no session has been paired against this checkout. **This on-disk layout is still the crux of why the gateway cannot be scaled horizontally today: `LocalStorageProvider`'s files exist only on the one machine's local filesystem, so a second gateway process has no access to them, and if the machine's disk is lost, every linked WhatsApp number is unrecoverable (has to be re-paired from scratch). The storage seam makes a future fix pluggable — it does not itself solve durability.**

**In-memory session registry:** `whatsapp-gateway/src/services/session.service.ts` holds everything else in plain `Map`s in process memory: `sessions` (id → `GatewaySession` metadata), `sockets` (id → live `WASocket`), plus `pendingOps`, `connectingSessions`, `reconnectTimers` as concurrency guards. None of this survives a process restart except by `restoreSessionsFromDisk()` re-reading directory names from `SESSION_PATH` — which restores *metadata* (each rediscovered session starts as `DISCONNECTED`) but only re-opens sockets if `RESTORE_SESSIONS_ON_BOOT=true`, and even then does so staggered (§3).

**Connection lifecycle in one sentence:** Django asks the gateway to connect a session; the gateway opens exactly one Baileys socket per session id, guarded against duplicates; on close it decides (via `config/reconnect.ts`'s rules) whether to retry with backoff or give up permanently; a logout from the phone wipes the session files and requires a fresh QR. Full detail in §3–§4.

---

## 3. Connection Lifecycle Audit

### Where sockets are created, destroyed, reconnected

| Action | Function | File |
|---|---|---|
| Create a Baileys socket | `createSocket(sessionId, hooks)` | `whatsapp-gateway/src/baileys/socket.factory.ts` |
| Destroy a socket (listeners off, then `socket.end()`) | `destroySocket(socket)` | same file |
| Decide reconnect vs. terminal from a close event | `handleConnectionUpdate(update, hooks)` | `whatsapp-gateway/src/baileys/connection.handler.ts` |
| Actually open/replace the socket for a session (single-flight) | `openSocket(sessionId)` | `whatsapp-gateway/src/services/session.service.ts` |
| Human-triggered connect (idempotent) | `connectSession(sessionId)` | same file |
| Schedule a delayed automatic retry | `scheduleReconnect(sessionId, statusCode)` | same file |
| Give up permanently (park session) | `giveUp(session, reason)` | same file |
| Owner-initiated disconnect (keep files) | `disconnectSession(sessionId)` | same file |
| Owner-initiated unlink (wipe files) | `unlinkSession(sessionId)` | same file |
| Boot-time rebuild of the in-memory map from disk | `restoreSessionsFromDisk()` | same file |

**There is exactly one code path that creates a socket** (`openSocket`, which always calls `createSocket`) and **exactly one code path that destroys one** (`destroySocket`, called from `openSocket`'s own duplicate-guard, `disconnectSession`, and `unlinkSession`). This matters: it means there is no second, forgotten socket-creation path that could bypass the safety checks below.

### Confirmed: no infinite retry loop

`config/reconnect.ts` caps automatic retries at `RECONNECT_MAX_ATTEMPTS = 6`, with exponential backoff (`RECONNECT_BASE_DELAY_MS = 5_000` doubling up to `RECONNECT_MAX_DELAY_MS = 300_000`, ±30% jitter via `RECONNECT_JITTER_RATIO`). After 6 attempts (roughly 5 minutes of trying), `giveUp()` parks the session in `SessionStatus.ERROR` with `needsManualReconnect = true` and a human must call `POST /sessions/:id/connect` again — which resets the counters (see `connectSession`'s explicit `session.reconnectAttempts = 0`). The file's own comment documents *why* this exists: an earlier version reconnected immediately and recursively, producing "335 login attempts against WhatsApp on one real number (~19/min) — the exact pattern WhatsApp bans numbers for" in a 17.8-minute run. **Confirmed fixed, and the fix is load-bearing — do not loosen these constants without re-reading that comment.**

### Confirmed: no recursive reconnect

`scheduleReconnect()` is the *only* function that calls `openSocket()` from a timer, and it always goes through `setTimeout`, never a direct recursive call. `openSocket`'s own failure path (`.catch(...)`) calls `scheduleReconnect` again rather than retrying inline. So each retry is deferred and counted — there is no call stack that could spin synchronously.

### Confirmed: no duplicate sockets

Two separate guards prevent this:
1. `connectingSessions` (a `Set<string>`) in `session.service.ts` — `openSocket()` refuses to run again for a session id that's already mid-open. The comment explains the exact failure mode this prevents: "two overlapping connect calls... each open a Baileys socket on the SAME credentials. WhatsApp resolves that by kicking both, each close schedules another reconnect, and the sockets multiply."
2. Inside `openSocket()`, before creating a new socket it looks up `sockets.get(sessionId)` and, if one exists, tears it down first via `withIntentionalClose` + `destroySocket` — so a reconnect never runs alongside the socket it's replacing.

`connectSession()` (the human-facing entry point) additionally short-circuits to a no-op if the session's status is already one of `ACTIVE_STATUSES` (`CONNECTING`, `QR_READY`, `CONNECTED`) — so a double-tapped "Connect" button in the app, or Django retrying a slow request, cannot start a second connect attempt either.

### A documented, subtle history bug already fixed — worth knowing about

`whatsapp-gateway/src/services/intentionalClose.ts` exists specifically to stop `onClose` from treating a close *we* caused (disconnect/unlink/duplicate-replace) as an unexpected drop that should be auto-reconnected. Its own doc comment explains a real bug that existed before: the suppression flag was set but the listener was detached before the close event could ever consume it, so it "stayed in the set forever," and the *next real* disconnect was silently swallowed — no reconnect scheduled, session shown as `CONNECTED` with a dead socket underneath. The fix scopes the flag to the exact teardown window (`withIntentionalClose(sessionId, teardown)`), consuming it exactly once. This is a good example of the kind of subtle bug distributed/concurrent session code invites — mentioned here because any storage refactor (§5) must not reintroduce a similar "flag never gets cleared" class of bug.

### Ban / restriction risk analysis (grounded in the current code)

| Risk | Present today? | Where it's handled |
|---|---|---|
| Infinite retry loop hammering WhatsApp | **Not present** — capped and backed off | `config/reconnect.ts`, `scheduleReconnect` |
| Duplicate sockets on one credential set | **Not present** — single-flight guard + teardown-before-replace | `connectingSessions`, `openSocket` |
| Recursive/synchronous reconnect | **Not present** — always via `setTimeout` | `scheduleReconnect` |
| Rapid QR regeneration (unattended screen looping QR forever) | **Not present** — capped at `QR_MAX_CYCLES = 3`, then `giveUp()` | `openSocket`'s `onQr` hook |
| Session corruption from concurrent writes to the same auth files | **Not directly present as a distinct guard**, but avoided as a side effect of the single-flight + single-process model: only one process ever touches a given session's files, and `useMultiFileAuthState`'s own `saveCreds` serializes writes within that process. **This protection would break the moment two gateway processes point at the same `SESSION_PATH`/session id** — see §7's "no two gateways own one session" requirement. |
| Fleet-wide "we look like an attacker" pattern (many sessions each individually within limits) | **Present and handled** — a gateway-wide circuit breaker (`COOLDOWN_FAILURE_THRESHOLD = 10` failures within `COOLDOWN_WINDOW_MS` (10 min) trips a `COOLDOWN_DURATION_MS` (30 min) freeze on *all* automatic reconnects, via `registerFailureAndCheckCooldown()`/`isInCooldown()`) | `session.service.ts` |
| Reconnect storm on process crash-loop / hot-reload | **Present and handled** — `restoreSessionsFromDisk()` only auto-reconnects if `RESTORE_SESSIONS_ON_BOOT=true` (off by default, explicitly because `tsx watch` restarting on every save would otherwise relogin every session on every save), and even then staggers connects `RESTORE_STAGGER_MS = 15_000` apart | `env.ts`, `session.service.ts` |
| Message-send rate limits (separate from connection bans, but same "don't look automated" concern) | **Present** — per-minute/day/recipient-hour caps plus a minimum inter-send interval with jitter, persisted to disk so a restart doesn't reset the counters | `services/rateLimiter.service.ts`, `config/rateLimit.ts` |

**Overall verdict:** the existing single-process design is careful and the documented history (335 logins in 17.8 minutes) shows the team already learned this lesson once. The greatest *remaining* risk is not in this code — it's in what a naive multi-gateway refactor could reintroduce: two gateway processes both believing they own the same session and racing to write the same auth files or both holding a live socket on the same WhatsApp identity. §7 addresses this directly with an ownership model.

---

## 4. Manual Logout / Device Unlink Handling

Two distinct paths reach the same cleanup, and it's worth being precise about which is which:

**A. Owner-initiated unlink (`DELETE /sessions/:id` → `unlinkSession(sessionId)` in `session.service.ts`):** the *gateway's own logic* proactively calls `socket.logout()` (telling WhatsApp "this device is unlinking"), then `destroySocket`, then `rm(sessionDir, { recursive: true, force: true })` to wipe the auth files, then marks the session `SessionStatus.UNLINKED` and removes it entirely from the in-memory `sessions` map. The whole sequence is wrapped in `withIntentionalClose` so the close this triggers isn't mistaken for a drop needing reconnect, and guarded by `pendingOps` so a second concurrent unlink/disconnect call is rejected with 409 rather than double-running.

**B. Phone-initiated logout (WhatsApp's own "Linked Devices" menu → `DisconnectReason.loggedOut` / status code 401 arrives on a `connection.update` close event):** `connection.handler.ts`'s `handleConnectionUpdate` special-cases this status code specifically (`statusCode === DisconnectReason.loggedOut` → `hooks.onLoggedOut()`) *before* the general retryable/terminal logic runs, so it's not just "one more terminal reason" — it gets its own hook. `session.service.ts`'s `onLoggedOut` handler then: cancels any scheduled reconnect, marks the session `SessionStatus.UNLINKED` with `needsManualReconnect = true` and an explanatory `lastError`, removes the socket from the registry, and — this is the important part, with its own detailed comment in the code — deletes the on-disk session directory (`rm(sessionDir, ...)`) exactly like the owner-initiated path does. The comment explains *why* this was added: without it, "the on-disk creds stay marked 'registered' even though WhatsApp has revoked them, so the next connect() resumes with those same dead credentials instead of starting a fresh pairing... produced exactly the observed loop: repeated 'Connection Failure' / 'logged out from handset' on every Connect tap, with no QR ever shown."

**What is correctly implemented today (matches the desired behavior in the task brief):**
- Destroy the socket — yes, both paths call `destroySocket`.
- Wipe auth files — yes, both paths `rm` the session directory.
- Require a new QR on next connect — yes, an empty directory means `useMultiFileAuthState` starts unregistered, so Baileys emits a fresh `qr` event.

**What is a gap vs. the fully desired behavior:**
- **"Notify Django"** — **not implemented as a push.** The gateway does not call back into Django when a phone-side logout happens; Django only finds out the next time it happens to call `GET /sessions/:id/status` (`gateway_client.get_status`), which forwards whatever `GatewaySession.status`/`lastError`/`needsManualReconnect` currently say. This is a polling model, not an event/webhook model. For a small SaaS this is an acceptable simplification (no webhook infrastructure needed), but it does mean there can be a window where WhatsApp has revoked the device and Django/the business owner doesn't know until the next poll or the next failed `send_text` (which does raise `GatewayError` with code `SESSION_NOT_CONNECTED`, and `apps/whatsapp/services.py`'s `_handle_send_failure` does create a `whatsapp_disconnected` notification at that point — so the "found out" path exists, just reactively).
- **Wipe via a storage provider abstraction** — **now implemented** (see §5). Both cleanup paths (owner-initiated unlink and phone-side logout) call `storageProvider.deleteSession(sessionId)` instead of a duplicated, hardcoded `fs/promises` `rm()` call.

---

## 5. Storage Layer Refactor (Phase 1) — SHIPPED

**Status: implemented.** The paragraphs below describe the actual code as it exists today in `whatsapp-gateway/src/storage/`, not a proposal. An earlier draft of this document sketched a different shape for this seam (`StorageFactory`, `saveAuthState`/`readAuthState`/`removeAuthState`, a `firestore.storage.provider.ts`) — that sketch was discarded during implementation in favor of the simpler shape below. Treat this section, not the old draft, as ground truth.

**The problem this solved:** "where session credentials live" (`useMultiFileAuthState` + local `fs` calls previously scattered across `socket.factory.ts` and `session.service.ts`) was hardwired to "the local disk of whichever machine runs this process." Making that swappable — without changing what the gateway does today — is a plain "extract an interface behind the concrete thing that already existed" refactor: no new behavior, same on-disk files, same format.

### `StorageProvider` interface (`whatsapp-gateway/src/storage/storage.provider.ts`)

```ts
export interface AuthStateHandle {
  state: AuthenticationState;
  saveCreds: () => Promise<void>;
}

export interface StorageProvider {
  /** Loads (or initializes) the auth state for a session. Mirrors Baileys' useMultiFileAuthState contract. */
  loadAuthState(sessionId: string): Promise<AuthStateHandle>;

  /** Permanently removes a session's stored credentials. */
  deleteSession(sessionId: string): Promise<void>;

  /** Lists session ids currently persisted, for boot-time restore. */
  listSessionIds(): Promise<string[]>;
}
```

Note this deliberately mirrors Baileys' own `useMultiFileAuthState` return shape (`{ state, saveCreds }`) rather than inventing a `save`/`read` pair of methods — `loadAuthState` is a one-call bootstrap that hands back a live `saveCreds` closure, exactly like Baileys expects, so `socket.factory.ts` barely changes shape.

### `LocalStorageProvider` (`whatsapp-gateway/src/storage/local.storage.provider.ts`)

A thin wrapper, not a rewrite — behavior is byte-for-byte what existed before the refactor:
- `loadAuthState(sessionId)` calls Baileys' own `useMultiFileAuthState(path.join(env.SESSION_PATH, sessionId))` directly and returns its result.
- `deleteSession(sessionId)` calls `rm(sessionDir, { recursive: true, force: true })`. This replaced two independent, copy-pasted `rm()` calls that used to live in `session.service.ts` — one inside `unlinkSession()`, one inside the `onLoggedOut` hook in `openSocket()` — both now call `storageProvider.deleteSession(sessionId)`.
- `listSessionIds()` calls `readdir(env.SESSION_PATH)` (returning `[]` if the directory doesn't exist yet), replacing the `readdir` call that used to be inline in `restoreSessionsFromDisk()`.

### Provider selection (`whatsapp-gateway/src/storage/index.ts`)

There is no `storage.factory.ts` file and no `getStorageProvider()` function — the module builds a single provider instance once at import time and exports it directly:

```ts
import { env } from "../config/env.js";
import { FirebaseStorageProvider } from "./firebase.storage.provider.js";
import { LocalStorageProvider } from "./local.storage.provider.js";
import type { StorageProvider } from "./storage.provider.js";

function buildProvider(): StorageProvider {
  switch (env.STORAGE_PROVIDER) {
    case "firebase":
      return new FirebaseStorageProvider();
    case "local":
    default:
      return new LocalStorageProvider();
  }
}

export const storageProvider: StorageProvider = buildProvider();
```

`socket.factory.ts` and `session.service.ts` both `import { storageProvider } from "../storage/index.js"` and call it directly — there's no per-call factory lookup, since the provider is stateless and picked once at boot.

`env.ts`'s zod schema has the new field: `STORAGE_PROVIDER: z.enum(["local", "firebase"]).default("local")` — note the branch is named **`firebase`**, not `firestore` (the old draft's name). A second new field, `MAX_SESSIONS: z.coerce.number().int().positive().default(30)`, was added in the same change (enforced in `session.service.ts`'s `createSession` with a `409 MAX_SESSIONS_REACHED` `ApiError`) — unrelated to storage, but shipped alongside it.

**Confirmed out of scope, as intended:** `connection.handler.ts`, `config/reconnect.ts`, `intentionalClose.ts`, and all reconnect/rate-limit logic were not touched. Only the "where do bytes live" calls moved behind the interface. `npx tsc --noEmit` passes clean after the change.

---

## 6. Firebase Placeholder (Phase 2) — Stub Only, Not Implemented

**Status: stub shipped, real implementation not started.** As with §5, this section now describes the actual stub file rather than a sketch.

**What Firebase/Firestore would solve, in plain terms:** Firestore is a hosted NoSQL database. Using it as a `StorageProvider` backend means session credentials live in the cloud instead of on one machine's disk — so any gateway instance (not just the one that originally paired the session) can read/write them, and a machine dying doesn't lose the WhatsApp pairing. This is *only useful once §7's multi-gateway design exists* — on a single gateway it's a strictly worse version of local files (network latency + a new paid dependency for no benefit). That's why this stays a stub, not a build-now item.

### `FirebaseStorageProvider` — stub (`whatsapp-gateway/src/storage/firebase.storage.provider.ts`, shipped)

The file that actually exists is named `firebase.storage.provider.ts` (not `firestore.storage.provider.ts` as an earlier draft proposed), and — unlike a stub that quietly no-ops — it **throws in its constructor**, so selecting `STORAGE_PROVIDER=firebase` today fails loudly at boot rather than silently behaving like local storage:

```ts
import type { AuthStateHandle, StorageProvider } from "./storage.provider.js";

export class FirebaseStorageProvider implements StorageProvider {
  constructor() {
    throw new Error(
      "FirebaseStorageProvider is not implemented yet. Set STORAGE_PROVIDER=local until Firebase support is built."
    );
  }

  loadAuthState(_sessionId: string): Promise<AuthStateHandle> {
    throw new Error("Not implemented.");
  }

  deleteSession(_sessionId: string): Promise<void> {
    throw new Error("Not implemented.");
  }

  listSessionIds(): Promise<string[]> {
    throw new Error("Not implemented.");
  }
}
```

The step-by-step plan for actually implementing this (Firestore document shape per session, Firebase Admin SDK dependency, env vars, removing the constructor `throw`) is written out in `whatsapp-gateway/docs/whatsapp_gateway_guide.md` §5 — that file is the maintained, current source for the Firebase implementation checklist; treat the schema sketch immediately below as historical context only, not a spec to follow verbatim (it predates the shipped interface and uses the old `authState`-blob shape rather than `AuthStateHandle`).

### Example Firestore collection schemas (design sketch only — for when Phase 2 is actually built)

```json
// collection: sessions/{sessionId}
{
  "sessionId": "b7e1...uuid",
  "businessId": "django-business-pk-123",
  "displayName": "Acme Bakery",
  "status": "CONNECTED",
  "phone": "923001234567",
  "ownerGatewayId": "gw-2",
  "authState": {
    "//": "Baileys creds — the actual key material. In practice this may be split",
    "//2": "into a subcollection (sessions/{id}/keys/{keyId}) rather than one huge doc,",
    "//3": "since Firestore documents cap at 1MiB and signal-protocol key stores grow."
  },
  "createdAt": "2026-08-01T10:00:00Z",
  "updatedAt": "2026-08-04T09:12:00Z"
}
```

```json
// collection: gateways/{gatewayId}   (see §8 for why this may live in Django/Postgres instead)
{
  "gatewayId": "gw-2",
  "url": "https://wa-gateway-2.onrender.com",
  "isActive": true,
  "capacity": 50,
  "currentSessionCount": 31,
  "lastHeartbeat": "2026-08-04T09:14:55Z"
}
```

```json
// collection: assignments/{sessionId}
{
  "sessionId": "b7e1...uuid",
  "gatewayId": "gw-2",
  "assignedAt": "2026-08-01T10:00:05Z",
  "status": "ACTIVE"
}
```

**Firebase Admin SDK, service accounts, and security rules are deployment concerns, addressed in §11 — nothing above requires touching them yet.**

---

## 7. Distributed Gateway Architecture

**The core idea in plain terms:** run several *identical* copies of the same gateway codebase (no per-instance code differences), each with a unique small identity (`NODE_ID`), and make sure that for any given WhatsApp session, exactly one of those copies is ever "in charge" of it at a time. The hard part of "distributed" here isn't the code — Baileys and Express don't care how many copies run — it's this ownership question, because two processes holding a live socket on the same WhatsApp credentials at once is exactly the duplicate-socket ban risk from §3, just spread across machines instead of within one.

### `NODE_ID`

A new required env var, e.g. `NODE_ID=gw-2`, read once at boot (added to `config/env.ts`'s zod schema). Used only for: (a) identifying this instance in logs, (b) the `ownerGatewayId`/`gatewayId` fields shown in §6 and §8, (c) a gateway refusing to act on a session it does not currently own (see below). It is **not** a load-balancer concept — nothing routes traffic by `NODE_ID` at the HTTP layer; Django decides which gateway URL to call before the request is ever sent (§8).

### Session ownership / assignment model

- **Assign-once, sticky:** when Django creates a new session (`_ensure_session_id` → `gateway_client.create_session`), Django — not the gateway — picks *which* gateway instance to send that request to (using the registry in §8), and records that choice (e.g. a `gateway_node_id` field alongside the existing `gateway_session_id` on the business model). Every subsequent call for that session goes to the same gateway, forever, unless a migration happens (below). This is "sticky" in the same sense as sticky sessions in web load balancing — the session doesn't bounce between gateways on every request, because bouncing is exactly what would let two gateways race to hold the same socket.
- **Migrate only on permanent death.** "Permanent death" is defined simply and conservatively: **a gateway instance is considered permanently dead only after it misses N consecutive heartbeats** (recommend N=3, at a heartbeat interval of e.g. 30s — so ~90s of silence) reported to the registry (§8). A single missed heartbeat (a slow request, a brief restart) is *not* death — it's noise, and reacting to noise by reassigning sessions is how you'd get two gateways both trying to own the same session during a normal deploy. Only crossing the missed-heartbeat threshold triggers Django to mark that gateway `is_active=False` and, for sessions previously assigned to it, allow a *new* assignment to a different live gateway. Because the dead gateway's process is (by definition, for this to be safe) actually gone, there is no real socket left running on the old side — but this is exactly where §6's shared storage backend earns its keep: if session state lived only on the dead gateway's local disk, migrating it anywhere is impossible; with Firestore-backed storage (§6, once built), a new gateway can read the same auth state and resume.
- **Avoiding double-ownership in the meantime:** until Phase 2 storage exists, sessions on a `LocalStorageProvider` gateway are **not migratable at all** — their auth files are physically only on that one machine. The honest, simple answer for Phase 1/local-storage sessions is: if that specific gateway machine is lost, those sessions must be re-paired (new QR) on whichever gateway they're reassigned to. This should be stated plainly to the user/business owner rather than hidden. Migration only becomes real once Phase 2's shared storage exists.

---

## 8. Django-Side Gateway Registry — Recommended Approach

**Recommendation: a small Django model/table, not environment variables and not Firebase, as the single source of truth for "which gateways exist."**

```python
# apps/whatsapp/models.py (new model)
class GatewayNode(models.Model):
    node_id = models.CharField(max_length=64, unique=True)   # matches the gateway's NODE_ID
    url = models.URLField()                                   # e.g. https://wa-gateway-2.onrender.com/api/v1
    is_active = models.BooleanField(default=True)
    capacity = models.PositiveIntegerField(default=50)        # soft cap on sessions this node should hold
    current_session_count = models.PositiveIntegerField(default=0)
    last_heartbeat = models.DateTimeField(null=True, blank=True)
```

**Why this over the alternatives:**

| Option | Pros | Cons |
|---|---|---|
| **Django model (recommended)** | Single source of truth already inside the system Django owns; a normal Django admin page gives instant visibility/editability with zero extra tooling; adding a gateway is one admin form submission, no redeploy of anything; trivially queryable for "pick the least-loaded active gateway" in plain Django ORM/SQL | One more table to migrate; Django itself becomes a dependency for gateway discovery (acceptable — it already is the only caller) |
| Environment variables (e.g. `GATEWAY_URLS=url1,url2,url3`) | Zero new code | Adding/removing a gateway requires editing env vars and **redeploying Django** — the exact friction the task brief flags as undesirable; no per-gateway state (heartbeat, capacity, active/dead) without inventing a second mechanism anyway |
| Firebase/Firestore registry | Real-time updates without polling; already needed if Phase 2 storage uses Firestore | A second source of truth split across two systems (Postgres for everything else, Firestore for just this) for no benefit — Django already has a database; adds a new paid dependency and new failure mode (Firestore down != Postgres down) purely for a low-write-volume table that changes rarely (gateways are added/removed by a human, not thousands of times a second) |

The heartbeat write is the one piece of new "push" behavior needed: each gateway instance, on a timer (e.g. every 30s), calls a small authenticated Django endpoint (`POST /api/whatsapp/gateway-heartbeat/` with its `NODE_ID`, current session count) which upserts `last_heartbeat`/`current_session_count` on its `GatewayNode` row. A cheap periodic Django management command (or the existing `apps/jobs` worker infra — `accountant_backend/apps/jobs/inprocess.py`/`runworker.py` already exist for background work) can then flip `is_active=False` for any node whose `last_heartbeat` is older than the 90s threshold from §7.

---

## 9. Local Development Setup — Running 3 Gateway Instances Locally

No code change is required for this — it's purely a matter of running the existing `whatsapp-gateway` three times with different env vars, each with its own `SESSION_PATH` and `PORT`, all pointed at the same local Django.

**Three separate `.env` files** (simplest, most explicit option):

```
# .env.gw1
PORT=3000
NODE_ID=gw-1
SESSION_PATH=./sessions-gw1
WHATSAPP_GATEWAY_JWT_SECRET=<same-secret-as-django>

# .env.gw2
PORT=3001
NODE_ID=gw-2
SESSION_PATH=./sessions-gw2
WHATSAPP_GATEWAY_JWT_SECRET=<same-secret-as-django>

# .env.gw3
PORT=3002
NODE_ID=gw-3
SESSION_PATH=./sessions-gw3
WHATSAPP_GATEWAY_JWT_SECRET=<same-secret-as-django>
```

Run each with dotenv-style loading pointed at the right file, e.g. three terminal tabs:

```bash
# terminal 1
env $(cat .env.gw1 | xargs) npm run dev   # or: npx dotenv -e .env.gw1 -- npm run dev

# terminal 2
env $(cat .env.gw2 | xargs) npm run dev

# terminal 3
env $(cat .env.gw3 | xargs) npm run dev
```

(On Windows/PowerShell, the equivalent is loading each `.env` file's variables into the session before `npm run dev`, or adding three `dev:gw1`/`dev:gw2`/`dev:gw3` scripts to `package.json` that each set `PORT`/`NODE_ID`/`SESSION_PATH` inline before invoking the existing dev script.)

Django's local settings then register three `GatewayNode` rows (`http://localhost:3000/api/v1`, `:3001`, `:3002`) via the Django admin or a management command/fixture — all pointing at `localhost`, all sharing the one local Postgres/SQLite Django already uses. This lets a student manually verify the assignment logic (§7/§8) end-to-end without any cloud dependency.

---

## 10. Architecture Diagrams

### 10.1 Current architecture (today, exactly as the code is)

```
                 +----------------------+
                 |   Django Backend     |
                 | apps/whatsapp/       |
                 |  gateway_client.py   |
                 +-----------+----------+
                              | HTTPS + short-lived
                              | session-scoped JWT
                              | (one hardcoded WHATSAPP_GATEWAY_BASE_URL)
                              v
                 +----------------------+
                 |  WhatsApp Gateway    |
                 |  (single Node proc)  |
                 |  session.service.ts  |
                 +-----------+----------+
                              | makeWASocket()
                              v
                 +----------------------+
                 |   WhatsApp servers   |
                 +----------------------+
                              |
                              v useMultiFileAuthState()
                 +----------------------+
                 | Local disk            |
                 | ./sessions/<id>/*.json|
                 +----------------------+
```

### 10.2 Phase 1 — storage interface, SHIPPED (this is the current architecture)

```
   session.service.ts / socket.factory.ts
                |
                v
     +-----------------------+
     |  storageProvider        |  (src/storage/index.ts — single instance,
     |  (StorageProvider)      |   built once at import time)
     +-----------+-------------+
                |  buildProvider() picks impl by STORAGE_PROVIDER env var
                v
     +-----------------------+
     | LocalStorageProvider   |  <- default; wraps useMultiFileAuthState +
     | (wraps existing fs     |    fs/promises rm/readdir, no behavior change
     |  calls, no behavior    |
     |  change)               |
     +-----------------------+
```

### 10.3 Phase 2 — Firebase-ready (stub shipped, not wired to real traffic)

```
     +-----------------------+
     |  storageProvider        |
     +-----------+-------------+
                |
        +-------+--------+
        v                v
+----------------+  +---------------------------+
| LocalStorage-   |  | FirebaseStorageProvider    |  <- stub only (§6):
| Provider        |  | (throws in its constructor |    throws immediately at
| (still default) |  |  until Phase 2 build)      |    boot, not lazily
+----------------+  +---------------------------+
```

### 10.4 Final distributed architecture (target state, after §7/§8 built)

```
+---------------------------+
|     Django Backend        |
| +-----------------------+ |
| | GatewayNode registry   | |   <- Postgres table (§8): id, url, is_active,
| | (Postgres model)       | |      capacity, last_heartbeat
| +-----------------------+ |
+-------------+--------------+
              | picks the assigned gateway URL per business (sticky, §7)
   +----------+--------------------+------------------+
   v                               v                  v
+----------+                 +----------+        +----------+
| Gateway   |                 | Gateway   |        | Gateway   |
| gw-1      |                 | gw-2      |        | gw-3      |
| (identical code,            | own NODE_ID)        |           |
+----+------+                 +-----+-----+        +-----+-----+
     |                              |                     |
     +--------------+---------------+-----------+---------+
                    v                            v
           +--------------------------------------------+
           |   Shared StorageProvider                     |
           |   (FirestoreStorageProvider,                 |
           |    Phase 2 - so any gateway can               |
           |    read a session's auth state)               |
           +--------------------------------------------+
```

### 10.5 Message flow (unchanged by any phase — included for completeness)

```
Business owner action -> Django view -> apps/whatsapp/services.py:send_text()
   -> gateway_client.send_text() [mints scoped JWT]
   -> HTTP POST /messages on the OWNING gateway
   -> message.controller.ts:sendText()
   -> rateLimiter.service.ts:checkAndConsume() [may delay/reject]
   -> message.service.ts -> socket.sendMessage() [Baileys] -> WhatsApp
```

### 10.6 Session assignment flow (new, §7/§8)

```
Django: business has no gateway_session_id yet
   |
   v
Query GatewayNode WHERE is_active=true ORDER BY current_session_count ASC LIMIT 1
   |
   v
Call POST /sessions on that gateway's url (scope=session:create)
   |
   v
Gateway returns { id }  ->  Django stores gateway_session_id AND gateway_node_id
   |
   v
Every future call for this business looks up its stored gateway_node_id,
resolves the GatewayNode.url, and calls that gateway - sticky, never re-picked
unless the "permanent death" flow (10.7) below runs.
```

### 10.7 Gateway recovery / failover flow (new, §7/§8)

```
Gateway gw-2 stops sending heartbeats
   |
   v
Django background job (apps/jobs) notices last_heartbeat older than
3 missed intervals (~90s)
   |
   v
GatewayNode(node_id="gw-2").is_active = False
   |
   v
Sessions previously assigned to gw-2 are NOT auto-migrated silently.
   |
   +- If storage is Local (Phase 1 only): session cannot move - flagged
   |  needs_manual_reconnect, business owner re-pairs (new QR) on whichever
   |  gateway Django assigns next.
   |
   +- If storage is Firestore (Phase 2 built): a new gateway can be assigned
      (10.6's flow, but reusing the existing sessionId), it reads the shared
      auth state, and resumes without a new QR.
```

---

## 11. Deployment Guide

### 11.1 Render — multiple gateway services

- Each gateway instance is a **separate Render Web Service**, one per `NODE_ID`, all built from the same `whatsapp-gateway` repo (check `whatsapp-gateway/package.json` for the exact build/start scripts, e.g. `npm install && npm run build && npm start`).
- **Per-service environment variables**: `PORT` (Render sets this for you via `$PORT` — the existing `config/env.ts` already reads `process.env.PORT` via zod, so no change needed), `NODE_ID` (unique per service, e.g. `gw-1`, `gw-2`), `SESSION_PATH` (if still on `LocalStorageProvider`, note Render's **filesystem is ephemeral on redeploy** on most plans — this is a real caveat: local-storage sessions on Render can be wiped by a redeploy unless a persistent disk add-on is attached), `WHATSAPP_GATEWAY_JWT_SECRET` (must be identical across all gateway instances and match Django's, since Django mints one token type trusted by whichever gateway receives it), `CORS_ALLOWED_ORIGINS` (leave empty in production per the existing code comment — Django is the only legitimate caller).
- **Health checks**: `whatsapp-gateway/src/routes/health.routes.ts` / `controllers/health.controller.ts` already exist and are mounted before the JWT middleware in `app.ts` (`app.use("/api/v1", healthRoutes)` runs before `app.use("/api/v1", requireDjangoJwt)`), so Render's health check can hit that route unauthenticated — worth confirming its exact path when configuring the Render service's health check field.
- **Free-tier sleep behavior caveat**: Render's free web services sleep after a period of inactivity and take roughly 30-60s to wake on the next request. For a WhatsApp gateway this is dangerous beyond the usual "slow first request" annoyance: if a session's socket needed to be alive to receive/send, a sleeping instance means that gateway's owned sessions are effectively offline until woken, and a wake-up after sleep is functionally similar to a restart (in-memory `sessions`/`sockets` maps are gone, `restoreSessionsFromDisk()` runs again). **Recommendation: paid "always-on" tier for any gateway instance that owns live sessions**, or explicitly accept that free-tier gateways are only for local dev/testing.

### 11.2 Firebase project setup (for when Phase 2 is implemented — not needed today)

- Create a Firebase project, enable **Firestore** (native mode, not Realtime Database — Firestore's document/collection model matches §6's schemas).
- Create a **service account** (Firebase Console -> Project Settings -> Service Accounts -> Generate new private key), and store the resulting JSON as a secret env var (e.g. `FIREBASE_SERVICE_ACCOUNT_JSON`, base64-encoded) on each gateway instance — never commit the key file.
- **Security rules**: since only the gateway's Admin SDK (server-side, using the service account, which bypasses security rules entirely) should ever touch this data, rules should default-deny all client access:
```
rules_version = '2';
service cloud.firestore {
  match /databases/{database}/documents {
    match /{document=**} {
      allow read, write: if false;   // Admin SDK bypasses this; no client access.
    }
  }
}
```

### 11.3 Django env vars for the gateway registry

No new env vars are strictly required for §8's model-based registry — it's just a database table, managed via Django admin or a migration + data fixture. If a bootstrap default is wanted for first-run convenience, a single optional `DEFAULT_GATEWAY_URL` (falling back to today's `WHATSAPP_GATEWAY_BASE_URL`) could seed the first `GatewayNode` row via a Django data migration, but this is optional polish, not a requirement.

---

## 12. Safety Rules Recap (Non-Negotiable Checklist)

| Rule | Enforced today? | Where |
|---|---|---|
| No infinite reconnect loop | Yes | `RECONNECT_MAX_ATTEMPTS` in `config/reconnect.ts`; `giveUp()` in `session.service.ts` |
| No recursive/synchronous reconnect | Yes | `scheduleReconnect()` always defers via `setTimeout`, never calls `openSocket` inline |
| No duplicate sockets on one session | Yes | `connectingSessions` single-flight guard + teardown-before-replace in `openSocket()` |
| No auto session recreation after logout | Yes | `onLoggedOut` sets `UNLINKED` + `needsManualReconnect=true` and wipes files; nothing auto-reconnects a logged-out session |
| Respect `DisconnectReason` (retryable vs. terminal) | Yes | `RETRYABLE_STATUS_CODES` / `TERMINAL_STATUS_CODES` in `connection.handler.ts`, with unrecognised codes defaulting to terminal (fail-safe direction) |
| Gateway-wide circuit breaker for fleet-level abuse pattern | Yes | `registerFailureAndCheckCooldown()` in `session.service.ts` |
| QR regeneration capped | Yes | `QR_MAX_CYCLES` |
| **No two gateway instances holding a live socket on the same session simultaneously** | **Not yet applicable (single instance today); MUST be designed for before §7 ships** | Requires sticky assignment (§7) + registry (§8); not yet code, this document's proposal |
| **No two gateway instances writing the same session's auth files concurrently** | **Not yet applicable; same as above** | Requires either sticky Local-storage ownership (never shared) or Firestore's own atomic writes once Phase 2 exists |

The last two rows are the ones a distributed rollout could violate if done carelessly — they are the entire reason §7 insists on "assign-once, sticky, migrate only on confirmed permanent death" rather than any form of dynamic/round-robin routing per request.

---

## 13. Final Report

### 13.1 Folder structure — Phase 1 shipped, Phase 2 is a stub

```
whatsapp-gateway/
  src/
    app.ts
    server.ts
    baileys/
      connection.handler.ts
      socket.factory.ts          (DONE - auth state via storageProvider.loadAuthState)
      message.receiver.ts
    config/
      env.ts                     (DONE - + STORAGE_PROVIDER, MAX_SESSIONS)
      rateLimit.ts
      reconnect.ts
    controllers/
      health.controller.ts
      message.controller.ts
      session.controller.ts
    services/
      session.service.ts         (DONE - rm() calls -> storageProvider.deleteSession();
                                    readdir() -> storageProvider.listSessionIds();
                                    createSession() enforces MAX_SESSIONS)
      intentionalClose.ts
      intentionalClose.test.ts
      rateLimiter.service.ts
      message.service.ts
    storage/                     (DONE - directory shipped)
      storage.provider.ts        (DONE - StorageProvider + AuthStateHandle interfaces)
      local.storage.provider.ts  (DONE - wraps existing fs logic, default provider)
      firebase.storage.provider.ts  (DONE as a stub - throws in constructor until built)
      index.ts                   (DONE - builds+exports the single storageProvider instance)
    middleware/
      errorHandler.middleware.ts
      jwt.middleware.ts
    routes/
      health.routes.ts
      message.routes.ts
      session.routes.ts
    types/
      qrcode.d.ts
      session.types.ts
    logger/logger.ts
    utils/
      ApiError.ts
      response.util.ts
      uuid.util.ts
  docs/
    whatsapp_gateway_guide.md    (DONE - maintained, current storage/Firebase how-to)
    WHATSAPP_GATEWAY_GUIDANCE.md (this file - broader audit + distributed roadmap, §7-13 still proposals)

accountant_backend/
  apps/whatsapp/
    gateway_client.py            (NOT STARTED - resolve target gateway URL per business, distributed phase only)
    services.py                  (NOT STARTED - pick/store gateway_node_id on first connect, distributed phase only)
    models.py                    (NOT STARTED - new GatewayNode model, distributed phase only)
    admin.py                     (NOT STARTED - register GatewayNode for admin visibility)
```

### 13.2 Files modified for the storage seam (Phase 1) — DONE

| File | Change actually made |
|---|---|
| `whatsapp-gateway/src/config/env.ts` | Added `STORAGE_PROVIDER: z.enum(["local", "firebase"]).default("local")` and `MAX_SESSIONS: z.coerce.number().int().positive().default(30)` to the zod schema. `NODE_ID` was **not** added — that's still §7's unbuilt distributed-phase proposal. |
| `whatsapp-gateway/src/baileys/socket.factory.ts` | Replaced the direct `useMultiFileAuthState(sessionDir)` call with `storageProvider.loadAuthState(sessionId)`. |
| `whatsapp-gateway/src/services/session.service.ts` | Replaced both duplicated `rm(sessionDir, ...)` calls (in `unlinkSession` and the `onLoggedOut` hook) with `storageProvider.deleteSession(sessionId)`; replaced the `readdir(env.SESSION_PATH)` call in `restoreSessionsFromDisk` with `storageProvider.listSessionIds()`; added a `MAX_SESSIONS` check (409 `MAX_SESSIONS_REACHED`) at the top of `createSession`. |

### 13.3 New files created for the storage seam (Phase 1 + Phase 2 stub) — DONE

| File | Reason |
|---|---|
| `whatsapp-gateway/src/storage/storage.provider.ts` | Defines the `StorageProvider` interface and `AuthStateHandle` type (§5) — the seam that makes storage swappable. |
| `whatsapp-gateway/src/storage/local.storage.provider.ts` | Phase 1 implementation wrapping today's exact local-disk behavior — no behavior change. Default provider. |
| `whatsapp-gateway/src/storage/firebase.storage.provider.ts` | Phase 2 stub (§6) — throws in its constructor; not implemented. |
| `whatsapp-gateway/src/storage/index.ts` | Builds and exports the single `storageProvider` instance, picked by `STORAGE_PROVIDER` env var. (Not a `storage.factory.ts` file/`getStorageProvider()` function — that was an earlier, discarded draft shape.) |
| `whatsapp-gateway/docs/whatsapp_gateway_guide.md` | Focused, maintained guide to the storage architecture, local config, and the future Firebase implementation checklist. |
| (distributed phase only, NOT started) `accountant_backend/apps/whatsapp/migrations/00XX_gatewaynode.py` | Django migration creating the `GatewayNode` table (§8). |

### 13.4 Step-by-step implementation plan (ordered, each phase shippable on its own)

1. **Phase 0 (done):** original single-gateway, local-storage, careful-reconnect implementation.
2. **Phase 1 — Storage seam. DONE.** `StorageProvider`/`LocalStorageProvider`/`index.ts` shipped, all three call sites in §13.2 rewired. Verified with `npx tsc --noEmit` (clean) and a read-through confirming `LocalStorageProvider` writes the exact same files, in the exact same layout, as the pre-refactor code — a pure refactor, zero observable behavior change. `MAX_SESSIONS` was added in the same change as a small, independent piece of scope (not part of the storage seam itself).
3. **Phase 2 — Firebase stub shipped; real implementation NOT STARTED.** `FirebaseStorageProvider` exists as a stub that throws in its constructor (§6) — selecting `STORAGE_PROVIDER=firebase` today fails loudly at boot, by design. Implementing it for real (Firestore document shape, Firebase Admin SDK dependency, Firebase project setup per §11.2) is future work; the checklist lives in `whatsapp-gateway/docs/whatsapp_gateway_guide.md` §5, not in this document. Still meant to run as a **single gateway instance** even once implemented — the value there is durability (session survives a disk loss/redeploy), not yet horizontal scaling. Verify (when built) by switching `STORAGE_PROVIDER=firebase` in staging and confirming pairing/reconnect/logout all still work identically to Phase 1.
4. **Phase 3 — Gateway registry in Django.** Add the `GatewayNode` model + admin + heartbeat endpoint (§8). Still only one real gateway instance registered — this phase just builds and tests the registry/heartbeat machinery in isolation.
5. **Phase 4 — Second gateway instance + sticky assignment.** Deploy a second gateway instance (different `NODE_ID`), register it, switch `_ensure_session_id` to pick a gateway via the registry and store `gateway_node_id` on the business (§7/§8/§10.6). New sessions start splitting across both instances; existing sessions keep working unchanged (their `gateway_node_id` can be backfilled to point at the original single instance).
6. **Phase 5 — Failover/migration flow.** Implement the missed-heartbeat -> `is_active=False` job (§10.7) and the (Phase-2-storage-only) reassignment logic. Test by deliberately killing a gateway instance in staging and confirming sessions on Local storage are correctly flagged for re-pairing, and sessions on Firestore storage correctly resume on a new instance.

Each phase leaves the system in a fully working state for existing users before the next phase starts — there is no phase that requires a "big bang" cutover.

### 13.5 Risk analysis table

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Phase 1 refactor subtly changes file paths/behavior | Low | Medium (could re-break the "session corruption" class of bug) | Keep `LocalStorageProvider` a thin wrapper with no logic changes; diff-test against current behavior before shipping. |
| Firestore write costs/latency under many sessions | Medium (once Phase 2 is real) | Low-Medium (cost, and creds.update fires often) | Batch/debounce the `saveCreds` closure's writes (`AuthStateHandle.saveCreds`, §5) if needed; Firestore pricing is per-document-write, so this is a real cost to monitor, not just a performance concern. |
| Two gateways racing on one session during a bad rollout of Phase 4/5 | Low if sticky assignment is respected; High if skipped | High (the exact ban-risk pattern documented in §3) | Never route a session's traffic based on anything other than its stored `gateway_node_id`; never auto-migrate on a single missed heartbeat (§7's 3-miss threshold exists specifically for this). |
| Render free-tier sleep taking a gateway offline unexpectedly | Medium (if free tier used in production) | Medium (sessions on that instance appear disconnected) | Paid always-on tier for any instance holding live sessions (§11.1). |
| Local-storage sessions becoming unrecoverable if that Render instance's disk is lost | Medium (ephemeral filesystem on redeploy) | High (business must re-pair WhatsApp, customer-facing disruption) | This is the primary argument for eventually doing Phase 2 — flag clearly to stakeholders that Phase 1 alone does not solve durability, only pluggability. |
| Registry becomes stale/wrong (e.g. `is_active=True` for a dead gateway) | Low with heartbeat job running; Medium if that job silently stops | Medium (requests routed to a dead gateway fail until noticed) | Heartbeat job should itself alert/log loudly if it hasn't run recently; simple to add to existing `apps/jobs` infra. |

### 13.6 Future scalability notes

- The registry model (§8) generalizes cleanly to "capacity-aware" assignment later (pick the gateway with the most spare `capacity - current_session_count`) without any schema change — that logic already fits in the one query described in §10.6.
- If message volume (not session count) ever becomes the bottleneck, that's a separate concern from this document's scope (which is entirely about *connection*/session distribution) — the existing `rateLimiter.service.ts` already rate-limits per session, so send throughput scales naturally with session count/gateway count.
- Nothing in this plan requires a message queue, container orchestrator, or shared cache — deliberately, per the brief's constraint and because the actual bottleneck this project has (or will have) is "how many WhatsApp identities can safely live across how many machines," which the registry + sticky-assignment model already answers without that extra machinery.
