# Accountant Agent Service

**An AI-powered bookkeeping assistant for small and medium businesses — built for how local shopkeepers, traders, and service providers already work: on WhatsApp, in Roman Urdu, over voice notes.**

This document describes a working MVP, not a concept. Every claim below is checked against the actual codebase; where something is a design decision not yet fully realized, or a future direction rather than a shipped feature, it is labeled as such explicitly.

---

## 1. The Problem

Most small and medium businesses in Pakistan run their books on paper registers, memory, or informal WhatsApp notes to themselves — or don't keep books at all. This isn't a training gap that a better UI fixes on its own: existing accounting software (QuickBooks, Zoho, and their local equivalents) is built around double-entry bookkeeping concepts, English-only interfaces, and form-based data entry — a fundamentally different mental model from how a shopkeeper actually thinks about their day ("Ali ne 500 diye," "20mm ka bill banao").

The result is a large population of active, real businesses with no verifiable financial record of their own operations — which matters beyond convenience: no ledger means no track record, and no track record means no access to formal credit, however creditworthy the underlying business actually is.

---

## 2. Why an AI Agent, Not a Form

Traditional accounting software asks the owner to translate their own business into the software's model: pick an account, select a category, enter a rate, save. That translation step is exactly where adoption fails for this segment — not because the owner can't use a phone, but because bookkeeping-as-data-entry is a second job layered on top of running the actual business.

An AI agent inverts this: the owner describes what happened, in their own words, in their own language, and the system performs the translation into structured accounting data. This is only viable because of what large language models make possible now — reliably parsing free-form natural language (including code-switched Roman Urdu/English) into structured intent — which is precisely the capability that didn't exist cheaply enough, or accurately enough, for a product like this to work five years ago.

The interesting engineering problem this creates is trust: a bookkeeping agent's entire value depends on the ledger being right, so "let the model write to the database" is not an acceptable design. Section 5 describes the specific mechanisms this system uses to get the convenience of natural-language input without inheriting the unreliability of a language model as the source of truth.

---

## 3. What the AI Agent Does Today

The following are implemented and working in the current codebase (`apps/chat`, `apps/sales`, `apps/customers`, `apps/documents`, `apps/image_info_extractor`, `apps/voice_transcriber`):

- **Understands natural-language bookkeeping instructions** in English, Urdu, and Roman Urdu, and turns them into structured drafts — a sale with line items, a payment, a new customer, a document request — rather than requiring a form.
- **Never writes to the ledger directly.** Every AI-proposed action is a draft the owner explicitly confirms before it becomes a real entry (see Section 5).
- **Resolves customer names with a bounded, explainable fuzzy-match algorithm** — not the model's own guess. A name is auto-linked only above a similarity threshold (0.85, `SequenceMatcher`-based) with a required margin (0.12) over the runner-up; below that, the system surfaces near-miss candidates and asks rather than silently attaching money to the wrong customer's ledger.
- **Grounds every answer in real data pulled from the database at request time** — customer balances, past item prices, recent entries, aggregate totals, and real WhatsApp delivery status are injected into the model's context on the turns that need them, rather than left to the model's memory of the conversation.
- **Extracts structured data from photographed bills and receipts** via vision-model OCR, matched against the business's existing customers and item history.
- **Transcribes voice notes server-side** directly into the owner's selected language — including writing Roman Urdu phonetically rather than defaulting to Urdu script — and can speak replies back using text-to-speech.
- **Generates and sends WhatsApp documents** (invoices, receipts, statements) as part of the same conversational flow, through the WhatsApp Gateway service.
- **Routes between a fast and a reasoning-tier language model** based on the complexity/risk of the request, rather than using one model size for everything (Section 5).

---

## 4. What Makes This Technically Difficult

This is not "a chatbot in front of a database." The parts of the system that were genuinely hard to get right, and where most of the engineering effort actually went:

- **Keeping a language model financially trustworthy without giving up its flexibility.** The core tension: natural language is inherently ambiguous, but a ledger entry cannot be. The system's answer is a hard separation between what the model is allowed to *propose* and what the system independently *verifies* before committing — detailed in Section 5.
- **Multilingual, code-switched, phonetically-inconsistent input.** Roman Urdu has no standard spelling — "tareek"/"tareekh"/"tarikh"/"tarkeeh" are all the same word. The system handles this with shape-based pattern matching (regex built around the phonetic structure of a word, not a fixed spelling list) rather than a lookup table that silently fails on a variant it's never seen.
- **Voice input reliability.** Audio transcription models can hallucinate plausible-sounding speech from silence or noise rather than reliably reporting "nothing was said" — a real failure mode this system hit and had to guard against with both prompt design and a hard pre-check that skips transcription entirely for audio too short to contain real speech.
- **Conversational state vs. database state staying consistent.** A chat is a linear history, but a draft can be edited, superseded by a newer one, or confirmed out of order relative to when it was shown. The system enforces "only the single most recent unconfirmed draft in a conversation is ever confirmable" at the database layer, not just in the UI — so a stale draft card cannot silently duplicate a sale or a WhatsApp send.
- **A stateful, unofficial WhatsApp integration.** Baileys (the WhatsApp Web protocol library used here) is not an official API — sessions can be remotely logged out, rate limits are self-enforced rather than provided, and reconnect behavior has to be deliberately governed (capped retries, staggered restores, QR-cycle limits) to avoid triggering WhatsApp's own anti-abuse detection. This is a real, ongoing operational risk of building on an unofficial protocol, not a solved problem — see Section 8.
- **Cost/latency control on every single message.** Every chat turn potentially touches multiple LLM calls (intent parsing, response composition for non-English replies) plus live status checks (e.g., WhatsApp connection state) — each of which has to degrade gracefully and stay fast, since this runs on every message a business sends, not just occasionally.

---

## 5. Safety & Validation Mechanisms That Actually Exist

These are implemented, not aspirational:

- **Draft-then-confirm, always.** The AI's structured output (`draft_bill`, `draft_payment`, `draft_action`, `draft_document`, `draft_customer`) is never written to the ledger on its own. A separate confirmation endpoint re-validates the customer, re-checks the amounts, and only then commits — the model's claims are treated as a proposal, not a fact.
- **The model is told real facts, not asked to recall them.** Current balances, item prices, recent entries, aggregate totals, and delivery status are queried from the database and injected into the prompt on the turns that need them, specifically so the model answers from ground truth instead of its own (unreliable) memory of the conversation.
- **Idempotency enforced at the database layer.** Confirming a draft uses an atomic conditional update (`draft_confirmed=False → True`) so a double-tap or a retried request cannot record the same sale or trigger the same WhatsApp send twice. A duplicate-sale time-window check catches the same failure mode from a different angle (an accidental repeat instruction, not just a repeated tap).
- **Superseded drafts are rejected server-side**, not just hidden in the UI — confirming an older draft once a newer one exists in the same conversation returns an explicit error rather than silently executing.
- **Prompt-injection boundaries are explicit.** Text originating from customer records or OCR'd documents is wrapped in an untrusted-data marker, with the model instructed to describe but never follow instructions found inside it.
- **Two-tier model routing by risk.** Routine conversation uses a fast/cheap model; anything touching money, an edit to an existing entry, or a document send is routed to a stronger reasoning-tier model.
- **Money values are re-parsed and range-checked server-side** (via `Decimal`, not trusted as a raw float from the model) before ever reaching the ledger, rejecting negative or unrealistic amounts.

**What this is not:** these are the specific, checkable safeguards that exist in the current code. They reduce — they do not eliminate — the risk of an incorrect entry from a misheard name or a misread OCR'd number, and the system is explicitly designed to *ask the owner* whenever it cannot resolve something confidently, rather than claim certainty it doesn't have.

---

## 6. Project Architecture

The system is a small set of focused services rather than one monolith — each with one job, independently scalable and independently replaceable.

```
                         ┌─────────────────────────┐
                         │        Mobile App       │
                         │(Flutter — Android/iOS)  │
                         │Chat UI · Voice · Camera │
                         │  Dashboard · Customers  │
                         └────────────┬────────────┘
                                      │ REST (JWT-authenticated)
                                      ▼
                         ┌─────────────────────────┐
                         │       Main Backend      │
                         │      (Django + DRF)     │
                         │  Accounts · Billing     │
                         │ Customers · Sales/Ledger│
                         │  Chat orchestration     │
                         │Documents · Notifications│
                         │Jobs(DB-backed taskqueue)│
                         └───┬──────────┬─────────┬┘
                              │          │         │
              internal API    │          │         │  internal API
       ┌──────────────────────┘          │         └──────────────────┐
       ▼                                 ▼                            ▼
┌─────────────────┐          ┌─────────────────────┐        ┌────────────────────┐
│ WhatsApp Gateway│          │   FastAPI Service   │        │   AI Providers     │
│(Node.js/Baileys)│          │  Document rendering │        │Groq · Google Gemini│
│ Session mgmt    │          │  (PDF/image), OCR   │        │ (chat, voice, OCR) │
│ Send/receive    │          └─────────────────────┘        └────────────────────┘
│ QR pairing      │
└─────────────────┘

                         ┌─────────────────────────┐
                         │        Database         │
                         │  SQLite in development; │
                         │PostgreSQL for production│
                         │  (see note below)       │
                         └─────────────────────────┘
```

- **Main Backend (Django + DRF)** is the system of record. It owns the ledger, customer records, billing/plan enforcement, and the chat pipeline that turns a message into a draft and, on confirmation, a real entry. It is the only service the mobile app ever talks to — the mobile app never calls the WhatsApp Gateway, an AI provider, or the rendering service directly.
- **WhatsApp Gateway (Node.js, Baileys)** owns the live WhatsApp Web session for each connected business number. Kept separate because a WhatsApp session is stateful and long-lived, with its own failure modes (remote logout, reconnects, rate limits) that shouldn't be coupled to the main API's request/response cycle.
- **FastAPI Service** handles stateless, CPU-bound work outside the main request path: rendering invoices/statements/receipts, and vision-based OCR extraction.
- **AI Providers (Groq, Google Gemini)** are called through a provider-agnostic integration layer — a fast-tier model for routine replies, a reasoning-tier model for anything touching money, a dedicated audio model for transcription, and vision models for OCR. The provider/model can be changed without changing the app's contract.
- **Database.** The backend runs on SQLite in local development for convenience. Production configuration is PostgreSQL-based (via `DATABASE_URL`), and the settings module explicitly refuses to start in production on SQLite unless an operator opts in deliberately — this guard exists in code today, not just as a stated intention.
- **Jobs.** Longer-running work (document rendering + WhatsApp delivery, voice transcription) is handed off to a database-backed task queue with a separate worker process, so a chat reply doesn't block on a slow render or a slow model call. This is a lightweight, custom queue built on Django's ORM — not a message broker like Celery/RabbitMQ — appropriate for current scale, and a known point that would need re-evaluation under significantly higher throughput.

This separation means the WhatsApp connection can fail and be reconnected without touching the ledger, the rendering service can be scaled independently, and a change in AI provider never requires a mobile app release.

---

## 7. Engineering Rigor: Testing

The backend carries a real, non-trivial automated test suite — not a placeholder:

| Area | Approx. test count |
|---|---|
| Chat pipeline (drafts, model routing, reply generation) | ~100 |
| Business-date parsing (Roman Urdu date phrases) | ~25 |
| Chat sync/offline history | ~17 |
| Accounts / production config checks | ~21 |
| AI agent capability layer | ~22 |
| Sales/ledger | ~19 |
| Documents | ~20 |
| Image OCR extraction & matching | ~10 |
| Customers | ~8 |
| Voice transcription | ~5 |
| Billing (trial, gating) | ~4 |

The FastAPI rendering service has its own suite (~20 tests). The WhatsApp Gateway has targeted tests around its reconnect-governance logic (e.g., not treating an intentional disconnect as a failure requiring retry).

**Honestly labeled gap:** the Flutter mobile app currently has no automated test suite (no widget/unit tests in the repository) — verification there has been manual. This is a real gap, not a hidden one.

---

## 8. User Convenience — What This Looks Like in Practice

| Capability | Status | Why it matters |
|---|---|---|
| Type, or speak — either works | Implemented | No forced workflow; a voice note between customers is realistic where a spreadsheet session at day's end is not. |
| Send a photo of a bill or receipt | Implemented | Handwritten/printed documents are read automatically, not retyped by hand. |
| AI replies in voice too | Implemented | Text-to-speech reply, in the owner's own language — not just text-in. |
| Roman Urdu, natively (not translated) | Implemented | The script/language millions of business owners actually use day-to-day. |
| WhatsApp-native delivery to customers | Implemented | Reaches the customer on the app they already have open — no new install, no manual attach-and-send. |
| Drafts, never silent writes | Implemented | The owner stays in control of their own ledger at all times (Section 5). |
| "Ali ko 500 diye" as a complete instruction | Implemented | The system does the accounting translation, not the owner. |

---

## 9. Commercial Packaging

The billing system's underlying mechanism — per-feature gating (`ai_chat`, `voice_reply`, `image_extraction`, `whatsapp_send`) with an optional numeric monthly cap per feature, enforced server-side per business subscription — is implemented and working today, including an automatic 7-day trial granted on signup (fully working, gated features unlocked immediately, with its own test coverage).

The specific commercial tiers below are the **intended go-to-market packaging** — a business decision layered on top of that mechanism, not yet the exact numbers seeded in the current build (which currently ships three placeholder tiers without configured message caps, pending final pricing):

| Plan | Price | Monthly AI Message Cap | Image OCR | AI Agent |
|---|---|---|---|---|
| **Free** | PKR 0 | Manual ledger entry only | — | Not included |
| **Starter** | PKR 700/month | 300 messages | Included | Full AI agent |
| **Growth** | PKR 1,400/month | 700 messages | Included | Full AI agent |
| **Custom** | Contact sales | Tailored to volume | Included | Full AI agent, priority support |

Every new business receives a **7-day free trial** of the full AI agent experience before any payment decision — implemented today, not just planned.

---

## 10. What Is Implemented vs. Partial vs. Planned

| Item | Status |
|---|---|
| Chat-driven draft → confirm → ledger flow | **Implemented** |
| Voice transcription + spoken replies | **Implemented** |
| Photo/OCR bill extraction | **Implemented** |
| WhatsApp document delivery | **Implemented** |
| Roman Urdu as a first-class language | **Implemented** |
| Two-tier AI model routing | **Implemented** |
| Fuzzy customer-name matching with ask-don't-guess fallback | **Implemented** |
| Draft supersession / idempotency protections | **Implemented** |
| 7-day free trial | **Implemented** |
| Feature gating + per-feature usage caps (mechanism) | **Implemented** |
| Specific commercial tier pricing/caps shown in Section 9 | **Planned** (mechanism exists; final numbers not yet seeded) |
| PostgreSQL in production | **Configured, guarded in code** (dev currently runs SQLite; production path exists and is enforced by settings, not yet exercised at scale) |
| Automated test coverage — backend | **Implemented**, substantial (Section 7) |
| Automated test coverage — mobile app | **Not yet implemented** |
| Credit-scoring / lending products on top of ledger data | **Future direction**, not built |

---

## 11. Long-Term Direction

The near-term product is bookkeeping automation, as described above. The longer-term direction — explicitly a future possibility, not a built feature — is what adoption at scale could produce as a byproduct: a population of small businesses that have never generated a structured financial record begin producing one automatically, as a side effect of ordinary daily use.

That structured, timestamped ledger data is the interesting long-term asset. Small-business access to formal credit in markets like Pakistan is constrained less by demand than by the absence of verifiable financial history for lenders to underwrite against. A real transaction ledger — not self-reported, not retrofitted after the fact — is the kind of input credit-scoring or embedded-lending products would need, and today it largely doesn't exist for this segment at meaningful scale.

The sequencing is deliberate and, we think, the more defensible path: earn adoption on a product that already stands on its own value as a bookkeeping tool, and only then evaluate financial-services extensions on top of a dataset that exists because the core product was good enough to be used daily — rather than starting from a lending thesis and trying to manufacture the data required to underwrite it. No lending, scoring, or financial-services functionality exists in the codebase today; this section describes a direction the product opens up, not a roadmap commitment.

---

## Changes Made in This Revision

1. **Verified every technical/architectural claim against the actual codebase** (Django app structure, WhatsApp Gateway source, billing models, test files) rather than describing intended behavior as if already true.
2. **Corrected the pricing section** — the specific PKR 700/1,400 tiers with numeric message caps are labeled as planned go-to-market packaging, distinct from the billing *mechanism* (per-feature gating + optional monthly caps), which is real and working today. The current seeded plans in code (Basic/Standard/Premium) don't yet carry those exact numbers.
3. **Confirmed and cited the 7-day trial as genuinely implemented** (with real test coverage), not just a stated policy.
4. **Added a Testing & Quality section** with real, approximate test counts pulled directly from the repository, and explicitly disclosed the mobile app's current lack of automated tests as a known gap.
5. **Corrected the "async task queue" description** to accurately describe a lightweight, Django-ORM-backed job queue with a worker process — not implying a message-broker-grade system like Celery/RabbitMQ.
6. **Corrected the PostgreSQL claim** to reflect what's actually in `settings.py`: a real, enforced production-config guard rather than a stated future intention.
7. **Added concrete, checkable detail to the "safety mechanisms" claims** — the actual fuzzy-matching thresholds (0.85 similarity, 0.12 margin), the atomic-update idempotency pattern, and the draft-supersession check — replacing generic assurances with specifics that can be verified in the code.
8. **Added a new "What Makes This Technically Difficult" section**, since the original draft asserted product value without explaining the underlying engineering problems (multilingual/phonetic input, model-hallucination guarding, unofficial-API operational risk, conversational-state consistency).
9. **Added an explicit Implemented / Partial / Planned status table** so no single claim in the document is ambiguous about its current state.
10. **Softened the closing section** from an unqualified "billion-dollar" framing into a clearly labeled future direction with no functionality claimed to exist, and removed language implying the data/lending thesis is already underway.
11. **Removed unverifiable or unfounded claims** that weren't traceable to the codebase (no invented user counts, revenue figures, or security guarantees were added; none were present in the prior draft to remove, but this pass confirmed none crept in here either).
