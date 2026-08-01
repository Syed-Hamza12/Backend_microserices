# AI Automation Layer

Think of this layer as the "brain" of the digital employee — everything else in the backend is
its hands (database, PDFs, WhatsApp). This doc covers how the brain is composed and how each
piece is prompted/constrained.

## 1. Components

| Component | Model/API | Role |
|---|---|---|
| Basic chat (intent detection, short replies, simple confirmations) | Groq — Llama 3.1/3.3 **8B** (Instant) | Cheap/fast path for low-complexity turns — greetings, yes/no confirmations, routing the message to see if it even needs deeper reasoning |
| Reasoning chat (drafting a Draft Bill, ambiguous instructions, multi-field extraction) | Groq — Llama 3.3 **70B** (Versatile) | Used only when the turn actually requires reasoning — deciding intent + producing a structured `draft_bill`, or resolving ambiguity |
| Vision extraction | Gemini Vision (free tier initially) | Reads a photo (challan/receipt/handwritten bill) → structured JSON |
| Clarification reasoning | Groq 8B first attempt; escalate to 70B only if the clarification itself is ambiguous | When Vision's output is missing/ambiguous fields, phrases a natural follow-up question and later reconciles the owner's answer into the draft |

Two providers, one reason each: Groq is used for the fast conversational/reasoning layer (cheap,
low-latency, good enough at structured-instruction-following for this use case); Gemini Vision is
used specifically because it's a strong off-the-shelf document/image understanding model — no
need to fine-tune anything for launch.

**Model-tiering strategy (cost control):** within Groq itself, split further by task weight —
8B for anything that doesn't require real reasoning (fast, generous free-tier token/rate limits),
70B only for the steps that need it (drafting a bill, resolving ambiguity). This keeps 70B call
volume — the tighter free-tier quota — low while pre-launch/testing (<5 businesses) stays
comfortably inside Groq's and Gemini's free-tier daily/per-minute limits. `groq_client.py` should
expose both model configs (e.g. `GROQ_MODEL_FAST` = 8B, `GROQ_MODEL_REASONING` = 70B) so the
caller picks per-call rather than hardcoding one model everywhere. Once usage outgrows free-tier
limits (target: onboarding beyond the first ~5 businesses), move to Groq's paid (pay-per-token)
tier — the same fast/reasoning split then directly controls $ spend, not just rate-limit headroom.

## 2. Text chat pipeline (Milestone 7)
```
1. Owner message arrives → apps/chat builds a prompt:
   - System prompt (fixed): role, tone, output-format contract (see Section 4), the owner's
     chosen language, instruction to keep replies short and WhatsApp-style
   - Business context: a compact summary (not a full DB dump) — e.g. "You are the accountant for
     <business_name>. Today's date is <date>. Recent customers: [...]. Today's sales total: ..."
     assembled by apps/chat from customers/sales, kept small and cheap to re-send every turn
   - Recent conversation history — last **N messages** (owner turns + AI responses combined, not
     N each). N defaults to **15** but is not hardcoded: it comes from
     `subscription.chat_history_limit_override` if set, else `business.subscription.plan.chat_history_limit`
     — both admin-editable in Django admin per plan or per individual business, no deploy needed
     (see `business_logic.md` Section 4a and `sqlite_database_attributes.md` `Plan`/`Subscription`)
   - The new message
2. Call Groq → parse response per Section 4's contract
3. If the reply implies a Draft Bill (owner said something like "bill Ali for 3000"), the model
   must return a draft_bill object, not just prose — see Section 4
4. Save ChatMessage, return to phone
```

## 3. Image extraction pipeline (Milestone 9)

**Where the Gemini call happens (deliberate deviation from the original "FastAPI only" plan):**
Django's `jobs` worker calls Gemini **directly** from `apps/image_info_extractor/gemini_client.py`
for the live path today, not through FastAPI's `/vision/extract`. Product reasoning: the owner
wants to A/B this in production — Gemini today, possibly a self-hosted/local vision model later —
and isn't sure yet which side (Django-direct vs. proxied through FastAPI) that will land on. So
**both paths are fully implemented**:
- `apps/image_info_extractor/gemini_client.py` (Django, live) — same extraction contract.
- `fastapi-service/vision/router.py`'s `POST /vision/extract` (FastAPI, built and working, just not
  currently called by the worker) — kept as the ready-made swap point for a future local model,
  per `ARCHITECTURE.md` Section 4.
Everything downstream of "raw extracted JSON" (customer matching, clarification-question
generation, draft_bill building) lives in `apps/image_info_extractor/services.py` either way, so
switching which path is live is a one-line change in the `jobs` worker's dispatch, not a rewrite.

```
1. Owner sends a photo → POST /api/chat/image/ (multipart) → job created (see
   backend_workflow.md Section 5)
2. Worker calls Gemini Vision (directly, per above) with a prompt asking specifically for:
   {date, amount, customer_name (best guess or null), line_items: [{item_name, quantity, rate}], raw_text}
   - if Gemini is unreachable/not configured (e.g. GEMINI_API_KEY unset), the job still completes
     "done" with a graceful text-only fallback reply, never a stuck "typing…" — see
     Section 5's guardrail.
3. Django's image_info_extractor (apps/image_info_extractor/services.py):
   - tries to match customer_name (fuzzy, difflib) against this business's Customer list
   - if amount/date missing, or customer_name is null/unmatched → calls Groq (8B first, 70B if
     more than one field is ambiguous) with a small clarification prompt: "The following was
     extracted from a photo: {json}. The field(s) {missing_fields} are missing or unclear. Write
     one short, natural question in {language} to ask the business owner to provide it." → sends
     that as the AI's chat reply, validated against the same output contract as Section 2/4
   - if everything resolves cleanly → builds a draft_bill directly (with a real customer_id and
     previous_balance, same as the text flow), and sends it as the reply
4. Owner's clarifying answer (plain text) re-enters the normal text chat pipeline (Section 2) —
   there is no separate "resume extraction" endpoint; the conversation context already has the
   partial extraction in history, so the next Groq call can complete the draft.
```

## 4. Output contract (both pipelines must conform to this — enforced by prompting + a
   Pydantic/DRF-serializer validation step on the response before saving)

```json
{
  "text": "string, always present, what's shown as the reply",
  "speech_text": "string or null — required if language is Roman Urdu and text will be spoken; native Urdu script",
  "draft_bill": {
    "customer_id": "string or null",
    "customer_name_guess": "string, only used if customer_id is null and needs owner confirmation",
    "previous_balance": "number",
    "total_amount": "number",
    "payment_received": "number"
  },
  "document_ready": {
    "document_type": "invoice|statement|receipt|report",
    "document_url": "string"
  }
}
```
`draft_bill` and `document_ready` are mutually exclusive and both optional — most replies have
neither (just `text`). If the model returns something that doesn't validate against this shape,
retry once with a stricter reminder prompt; if it fails twice, fall back to a plain-text apology
reply rather than surfacing a broken card to the owner.

## 5. Guardrails
- The AI never writes to the database directly — see `business_logic.md` Section 6, "Draft Bill is
  never a separate data model." Every AI-proposed change requires the owner's explicit
  "Confirm and Send" tap.
- The AI only ever sees this business's own data — the context-assembly step in Section 2 must
  filter by `business_id`, same as every other query in the system (`claude_rule.md` Section 2).
- Feature-gate check (Milestone 6/`business_logic.md`) happens **before** any Groq/Gemini call —
  never spend money on a call for a business that isn't entitled to it.
- Cost control: keep the business-context summary small (Section 2) and cap conversation history
  sent per call to the plan/business's configured `chat_history_limit` (default 15, owner + AI
  combined — admin-editable per plan or per business, see Section 2); log token usage per call so
  `UsageCounter` and cost-per-business can later be reconciled against actual Groq/Gemini billing
  if needed.
- If Groq or Gemini is unreachable/times out, return a graceful `text`-only error reply
  ("Sorry, I couldn't process that right now — please try again.") — never leave the phone's
  "typing…" indicator stuck (matches the try/catch guidance already in
  `BACKEND_INTEGRATION_GUIDE.md` Section 7a).

## 6. Language handling
- `Business.language` drives both the prompt's requested output language and whether `speech_text`
  needs the native-script conversion.
- English and Urdu: `text` and `speech_text` can be identical (or `speech_text` omitted, phone
  falls back to `text`).
- Roman Urdu: `text` stays Roman Urdu (what's displayed), `speech_text` must be native Urdu script
  (what's spoken) — this was a real bug found in on-device TTS during testing, so treat it as a
  hard requirement, not a nice-to-have.
