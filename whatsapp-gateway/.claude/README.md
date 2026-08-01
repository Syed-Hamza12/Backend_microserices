# WhatsApp Gateway

## What This Is

A standalone microservice that connects business WhatsApp accounts and exposes simple REST APIs to send/receive messages. It does **only** WhatsApp communication — no AI, no CRM, no billing, no business logic. Those live in your main Django backend and talk to this gateway over HTTP.

```
Django Backend  →  WhatsApp Gateway  →  Baileys  →  WhatsApp
```

Think of it like a payment gateway, but for WhatsApp: your main app calls it, it does one job, it reports back.

## Tech Stack

| Purpose | Choice |
|---|---|
| Language | TypeScript |
| Runtime | Node.js |
| Framework | Express |
| WhatsApp | Baileys |
| Auth (now) | API Key header |
| Auth (later, only if needed) | JWT |
| Session storage (now) | Local disk — fine up to ~1000 sessions on one server |
| Session storage (later) | Postgres (metadata) — only if you outgrow one server |

**Rule of thumb:** don't add Postgres/Redis/Docker until local disk + single server actually becomes a bottleneck. At 1000 users it won't.

## Scope

**In scope**
- QR login
- Send / receive text messages
- Disconnect / reconnect / unlink
- Session recovery after restart

**Never in scope (belongs in Django backend)**
- AI, CRM, billing, customer data, auth system, dashboards, analytics

## One Company = One Session

Every connected WhatsApp account gets a `gatewaySessionId` (UUID). Never use phone numbers as identifiers internally — a business could change numbers, the UUID doesn't.

## Session States

```
CREATED → CONNECTING → QR_READY → CONNECTED → DISCONNECTED → RECONNECTING → CONNECTED
                                        ↓ (user unlinks from phone)
                                    UNLINKED
```

See `ARCHITECTURE.md` for folder structure, API endpoints, and security rules.
See `SPRINT.md` for what to build next.
See `CLAUDE_RULES.md` for how Claude Code should behave in this repo.