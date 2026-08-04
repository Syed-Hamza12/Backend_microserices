# WhatsApp Gateway — Storage Architecture Guide

## 1. What changed

Session credentials (Baileys auth state — `creds.json` plus signal keys) used
to be read and written directly with `useMultiFileAuthState()` inside
`src/baileys/socket.factory.ts`, and session-directory cleanup was done with
raw `fs` calls in `src/services/session.service.ts`.

That direct filesystem access is now behind a `StorageProvider` interface.
Nothing about *how* sessions are stored today has changed — it's the same
JSON files, in the same `SESSION_PATH` directory, in the same format. The
only difference is that the rest of the app now calls a provider instead of
touching `fs` directly, so a different backend (Firebase) can be swapped in
later by implementing one class and changing one environment variable.

No connection lifecycle, reconnect, or QR logic was touched.

## 2. The abstraction

```
src/storage/
  storage.provider.ts          — StorageProvider interface + AuthStateHandle type
  local.storage.provider.ts    — today's implementation (JSON files on disk)
  firebase.storage.provider.ts — placeholder, throws until implemented
  index.ts                     — picks a provider based on STORAGE_PROVIDER
```

```ts
interface StorageProvider {
  loadAuthState(sessionId: string): Promise<{ state, saveCreds }>;
  deleteSession(sessionId: string): Promise<void>;
  listSessionIds(): Promise<string[]>;
}
```

Three callers use `storageProvider` instead of `fs`/`useMultiFileAuthState`:

- `src/baileys/socket.factory.ts` → `loadAuthState()` when opening a socket.
- `src/services/session.service.ts` → `deleteSession()` on unlink and on
  phone-side logout; `listSessionIds()` on boot to restore known sessions.

## 3. Configuring the local provider (current default)

Nothing to do — it's the default. Relevant env vars:

```
STORAGE_PROVIDER=local
SESSION_PATH=./sessions
```

`SESSION_PATH` is the same setting as before; each session still gets its own
subdirectory (`./sessions/<sessionId>/`) holding Baileys' standard files.

## 4. Setting up Firebase for the future (not implemented yet)

When you're ready to move off local disk (e.g. deploying somewhere with an
ephemeral filesystem), the plan is:

1. **Create a Firebase project** at https://console.firebase.google.com.
2. **Enable Firestore** (Native mode) as the store for session documents.
3. **Create a service account**: Project Settings → Service Accounts →
   Generate new private key. This downloads a JSON credentials file.
4. **Do not commit that file.** Store its path or contents via environment
   variables (see below), the same way `WHATSAPP_GATEWAY_JWT_SECRET` is
   handled today.

## 5. Connecting Firebase to the gateway (future work, not yet built)

This is the implementation checklist for whoever picks this up:

1. Add the Firebase Admin SDK: `npm install firebase-admin`.
2. Implement `src/storage/firebase.storage.provider.ts`:
   - `loadAuthState(sessionId)`: read/initialize the session's Firestore
     document (or fall back to Baileys' `initAuthCreds()` if it doesn't
     exist yet), and return a `saveCreds` closure that writes the updated
     creds + signal keys back to that document on every `creds.update`
     event. Model the document shape after what `useMultiFileAuthState`
     writes to disk (`creds.json` plus one file per signal key) — same
     fields, just stored as one Firestore document instead of files.
   - `deleteSession(sessionId)`: delete that Firestore document.
   - `listSessionIds(sessionId)`: list document IDs in the sessions
     collection.
3. Remove the `throw` in the `FirebaseStorageProvider` constructor once the
   above is implemented.
4. Add the needed env vars (suggested names):
   ```
   STORAGE_PROVIDER=firebase
   FIREBASE_PROJECT_ID=...
   FIREBASE_CLIENT_EMAIL=...
   FIREBASE_PRIVATE_KEY=...
   ```
   Add these to the `envSchema` in `src/config/env.ts`, required only when
   `STORAGE_PROVIDER=firebase` (e.g. with Zod's `.superRefine`).
5. Update `.env.example` with the new placeholders.

No other file needs to change — `socket.factory.ts` and `session.service.ts`
already talk only to the `StorageProvider` interface.

## 6. Environment variables reference

| Variable | Default | Purpose |
|---|---|---|
| `STORAGE_PROVIDER` | `local` | `local` or `firebase` (firebase not yet implemented) |
| `SESSION_PATH` | `./sessions` | Directory for local provider session files |
| `MAX_SESSIONS` | `30` | Hard cap on concurrently registered sessions |
| `FIREBASE_PROJECT_ID` | — | Future — required only when `STORAGE_PROVIDER=firebase` |
| `FIREBASE_CLIENT_EMAIL` | — | Future — required only when `STORAGE_PROVIDER=firebase` |
| `FIREBASE_PRIVATE_KEY` | — | Future — required only when `STORAGE_PROVIDER=firebase` |

## 7. Switching providers later

Once Firebase support is implemented (section 5):

1. Set the Firebase env vars.
2. Change `STORAGE_PROVIDER=firebase`.
3. Restart the gateway.

No code changes, no business-logic changes. Existing local sessions do
**not** automatically migrate to Firestore — that would need a one-off
migration script reading local JSON files and writing them through the new
provider, which is out of scope until Firebase is actually implemented.

## 8. Testing

**Today (local provider, default):**

```
npm run dev
```

1. `POST /sessions` → creates a session.
2. `POST /sessions/:id/connect` → starts a socket, generates a QR.
3. `GET /sessions/:id/qr` → scan with WhatsApp.
4. `GET /sessions/:id/status` → should reach `connected`.
5. Check `./sessions/<id>/` on disk — same files as before this refactor.
6. Restart the gateway (`RESTORE_SESSIONS_ON_BOOT=true` if you want it to
   reconnect automatically) and confirm the session is registered from disk.
7. `DELETE /sessions/:id` → confirm the session directory is removed.

**Max sessions cap:**

Set `MAX_SESSIONS=1`, create one session, then try to create a second —
expect a `409 MAX_SESSIONS_REACHED` error.

**Provider selection:**

Setting `STORAGE_PROVIDER=firebase` today should make the gateway fail fast
on startup with "FirebaseStorageProvider is not implemented yet" — this is
intentional until section 5 is built, so a misconfiguration can't silently
fall back to local storage.
