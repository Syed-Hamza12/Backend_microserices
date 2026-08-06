# How the "Agent" System Works

Written 2026-08-04 for future-me (or whoever maintains this next). This is a
plain explanation of how AI/agent behavior is implemented in this repo, based
on actually reading the code — not a description of what agent frameworks
usually look like.

## 1. The one-sentence answer

**There is no agent framework here.** No LangChain, no LangGraph, no
OpenAI/Anthropic "tools" API, no `AgentExecutor`. It's plain Python:
one LLM call that returns JSON, plus a hand-written planner/executor that
decides what to actually *do* with that JSON. Check `requirements.txt` — you
will only find `groq`, `google-genai`, `fastapi`, Django/DRF. No agent
libraries are installed.

## 2. The two halves

There are two Django apps involved, and they do very different jobs:

- **`apps/chat/`** — talks to the LLM (Groq, which is OpenAI-API-compatible),
  builds the prompt, gets back a JSON reply.
- **`apps/agent/`** — a deterministic Python "planner + executor" that reads
  that JSON and decides which internal functions ("capabilities") need to run
  to actually fulfill it (look up a customer, generate a PDF, send it on
  WhatsApp, etc).

The LLM never calls a tool directly. It just fills out a JSON form. Python
then decides what that form means and does the work.

## 3. Walking through one real request

Say an owner texts: *"send Ali his invoice"*.

1. **`apps/chat/services.py: generate_reply()`** — builds the message list
   (system prompt + recent chat history + the new message), picks which Groq
   model to use (`select_model_tier`), and calls Groq.
2. **`apps/chat/groq_client.py: call_groq()`** — a thin wrapper around
   `groq.Groq().chat.completions.create(...)`, forcing
   `response_format={"type": "json_object"}` so the reply is guaranteed to be
   parseable JSON. This is a *plain chat completion*, not OpenAI-style
   function/tool calling.
3. The model returns something like:
   ```json
   {"text": "On it...", "draft_document": {"doc_type": "invoice", "customer_id": null, "customer_name": "Ali"}}
   ```
   The system prompt (`apps/chat/prompt.py`, `OUTPUT_CONTRACT_INSTRUCTIONS`)
   is what tells the model this exact JSON shape is required — this is the
   entire "tool schema" the model ever sees. It has no idea capabilities.py
   exists.
4. **`apps/chat/services.py: apply_safe_document_send()`** hands that
   `draft_document` dict to `apps/agent/planner.py: plan_from_reply()`.
5. **The planner** (`apps/agent/planner.py`) does **backward chaining**: it
   knows it needs a `document_ref` (produced by
   `generate_document_from_entry`), which needs an `entry_id` (produced by
   `find_latest_entry`), which needs a `customer_id` (produced by
   `find_customer`). It walks backward from the goal to the dependencies and
   produces an ordered list of steps: `find_customer → find_latest_entry →
   choose_rendering_format → generate_document_from_entry →
   send_whatsapp_document`. Each step is also *resolved* against the real
   database right away (e.g. actually looking up "Ali" in `Customer`).
6. **The executor** (`apps/agent/executor.py: execute_plan()`) runs each
   step's `execute()` function in order, stops immediately if a step fails or
   is asynchronous (WhatsApp sending happens on a background job, not
   in-request).
7. Every step of this is written to an `AgentGoal` row (`apps/agent/models.py`)
   via `GoalManager` (`apps/agent/goals.py`) — this is how the system
   "remembers" a goal is still waiting on a WhatsApp delivery confirmation
   that will arrive later, asynchronously, from a totally separate request
   (the job worker calls `GoalManager.handle_event(...)` when that happens).

## 4. Where "tools" are actually defined

`apps/agent/capabilities.py`. This is the entire tool registry — about 9
capabilities: `find_customer`, `find_latest_entry`,
`choose_rendering_format`, `get_balance`, `generate_document_from_entry`,
`generate_document_from_range`, `send_whatsapp_document`, `record_payment`.

Each capability is a `Capability` dataclass with:
- `required_inputs` / `outputs` — sets of string keys, used by the planner to
  chain steps together.
- `risk_tier` — `"safe"`, `"financial"`, or `"dangerous"`.
- `resolve(business, conversation, have)` — read-only: validates/looks up
  real data, can return a `Clarification` if something's ambiguous or missing.
- `execute(business, resolved)` — does the actual side effect (DB write, send
  a message, etc), returns an `Outcome`.

**The important safety property, stated directly in the file's own comment:**
a capability that isn't in this `CAPABILITIES` dict cannot be reached by the
planner or executor, no matter what the LLM outputs. There is no
`delete_entry` or `bulk_*` capability. The LLM literally cannot cause an
action that doesn't have a hand-written Python function for it here. This is
the main reason this system is *safer* than "give the LLM raw tool-calling
and a database connection."

## 5. Multi-agent handoff — there isn't one

There's no agent-to-agent conversation, no orchestrator agent delegating to
sub-agents. The two things that could be mistaken for that:

- **Model tier selection** (`apps/chat/services.py: select_model_tier`) —
  just picks between a fast 8B Groq model and a bigger 70B "reasoning" model
  based on regex-detected intent and language. Not agent collaboration, just
  "which model do I call this turn."
- **Async job handoff** — `send_whatsapp_document`'s `execute()`
  (`apps/agent/capabilities.py`) queues a background job and returns
  `waiting_on={"event": "document_delivery", ...}`. The executor stops there.
  Later, when the job finishes (success or failure), the job worker calls
  `GoalManager.handle_event()` (`apps/agent/goals.py`) which finds the
  matching `AgentGoal` row (status `awaiting_verification`) and closes it out.
  This is a producer/consumer handoff between a web request and a background
  worker — not two agents talking.

## 6. Where state lives

Everything is in Postgres/SQLite via Django ORM. No Redis, no vector store,
no in-memory session state.

- **Chat history**: `ChatMessage` rows (`apps/chat/models.py`), fetched by
  `_recent_history()` and turned back into an OpenAI-style `messages` list by
  `apps/chat/prompt.py: build_messages()`. The model literally re-reads its
  own past JSON replies each turn — that's the entire "memory" mechanism.
- **Business context** (customers, balances, recent entries): queried fresh
  from the DB every single call and stuffed into the system prompt as text
  (`apps/chat/prompt.py: build_business_context` / `build_entry_context`).
  No RAG, no embeddings — just SQL queries interpolated into a prompt string.
- **In-flight agent workflow state**: `AgentGoal.plan` (a JSONField — an
  ordered list of `{"capability", "status", "output"}` dicts), written only
  through `GoalManager`. This is what survives between the synchronous
  request and the later async WhatsApp delivery callback.

## 6.5. Which model actually runs — Gemma (fast tier) vs `llama-3.3-70b-versatile`

**Updated**: the chat "fast" tier moved from Groq's `llama-3.1-8b-instant` to Google's Gemma
(`settings.GOOGLE_FAST_MODEL`, default `gemma-4-31b-it`) via the Generative Language API. The
reasoning tier is completely unchanged — still Groq's `llama-3.3-70b-versatile`.

| Setting | Model | Nickname in code | Provider |
|---|---|---|---|
| `GOOGLE_FAST_MODEL` | `gemma-4-31b-it` (default) | "fast" tier | Google, via `apps/chat/google_client.py` |
| `GROQ_MODEL_REASONING` | `llama-3.3-70b-versatile` | "reasoning" tier | Groq, via `apps/chat/groq_client.py` |

`apps/chat/services.py: _call_model(tier, messages)` is the only dispatch point — `tier ==
"fast"` → `google_client.call_gemma_planner()`, `tier == "reasoning"` → `groq_client.call_groq(reasoning=True)`.
Both providers are deliberately isolated from each other (no shared client, no shared retry
state) so a Google outage cannot affect Groq calls or vice versa. Key rotation for the Google
side reuses `settings.GEMINI_API_KEYS` and the canonical client in
`apps/integrations/google_genai_client.py` — the same rotation/model-fallback code
`apps/image_info_extractor/gemini_client.py` (vision OCR) uses, not a second copy of it.

`GROQ_MODEL_FAST`/`llama-3.1-8b-instant` still exists as a setting and is still used, but only by
`apps/image_info_extractor/clarification.py`'s own separate two-tier split for OCR
follow-up-question wording — an unrelated, narrower flow outside `select_model_tier()`'s routing,
left untouched by this change.

**As of the model-routing refactor described here, there are now TWO
separate calls in a normal chat turn, each with its own job and potentially its own provider —
not one call doing everything:**

1. **The JSON/intent step** — understands the message, extracts entities,
   fills out the draft_bill/draft_action/draft_document JSON contract.
   Always decided by `select_model_tier()` (`apps/chat/services.py:300-322`)
   purely on **intent complexity**, via `prompt.needs_reasoning()` — never on
   language anymore:
   - Gemma (`GOOGLE_FAST_MODEL`) when the message doesn't look like a bill/edit/
     document request (plain questions, small talk) — regardless of language.
   - `llama-3.3-70b-versatile` when `needs_reasoning()` detects billing/
     editing/document intent — regardless of language.
2. **The response-writer step** — ONLY for `language in ("ur", "roman_ur")`, still Groq 70B,
   `_write_final_reply()` (`apps/chat/services.py:370-430`) sends
   `llama-3.3-70b-versatile` the **owner's original message** plus a plain,
   Python-built **execution summary** of what was actually decided/done —
   never the JSON step's own drafted sentence. It composes a brand-new reply
   from those facts (`prompt.RESPONSE_WRITER_ROMAN_UR` / `RESPONSE_WRITER_UR`).
   No system prompt, business context, chat history, or JSON contract reaches
   it either way. It has zero ability to change intent, invent facts, or
   influence planning — it can only phrase what the execution summary says
   already happened.

   **Important distinction — this is NOT "rewrite the 8B draft."** The JSON
   step's `"text"` field is discarded outright for `ur`/`roman_ur` turns; it
   is stored as a DB placeholder for one moment (so `AgentGoal`'s FK has a
   message row to point at) and is always overwritten before the request
   returns — see `generate_reply()`, `apps/chat/services.py:433-544`. **No
   code path returns 8B's (or the JSON-step model's) own prose to the owner
   for these two languages.** The execution summary that 70B actually reads
   comes from `_build_execution_summary()` (`services.py:328-367`), built
   from the JSON contract's own structured fields — `draft_action.summary`,
   `draft_document.summary`, or a hand-assembled description of
   `draft_bill`'s amounts/customer/status — never from `"text"`, *except*
   for a plain question/answer turn with no draft at all, where there is no
   structured fact to summarize and the JSON step's `"text"` is used purely
   as raw CONTENT for 70B to re-express in its own words (still never shown
   directly).

   Two exceptions where the response-writer step is skipped entirely,
   because the final text is already correct and already localized —
   running the writer over it would add cost for no benefit:
   - The agent layer (`apply_safe_document_send()`) already wrote a
     Python-template, per-language string (`_SENDING_TEXT` in
     `apps/agent/capabilities.py`) or a `Clarification` message — see
     `apply_safe_document_send()`'s return value in `services.py:547-589`.
   - The chat call failed outright — `_fallback_reply()`'s canned,
     already-localized `FALLBACK_REPLIES` text is used as-is.

**Why this changed:** the 8B model's Urdu/Roman Urdu *writing* quality is
genuinely bad enough that it once produced "پاپ کا جو 1 ڑڑ خَد 20 مۉّد" — not
words — to a real owner. That used to be "fixed" by forcing 70B onto the
**entire** JSON+text call for every Urdu/Roman Urdu message, which meant the
full system prompt (business context, chat history, the whole JSON contract
— easily 1-2k+ tokens) was sent to the expensive model even for "hi, kya
haal hai". A first pass at fixing this had 70B merely *rewrite* the JSON
step's own draft sentence — cheaper, but still let the JSON-step model's
phrasing and structure leak into the final reply. The current design goes
further: 70B never sees that sentence at all for these languages. It only
ever sees the owner's message and a plain fact summary, and composes the
reply from scratch — the JSON generation itself goes to whichever tier the
*intent* actually needs, same as English always has, and the wording the
owner reads is 100% attributable to the response-writer step.

**Call-count picture, per turn:**
- English, no billing intent: **1 call, 8B.** (unchanged)
- English, billing/document intent: **1 call, 70B.** (unchanged)
- Roman Urdu / Urdu, no billing intent, no agent auto-execution: **1 call,
  8B** (JSON step) **+ 1 small call, 70B** (writer step, composing from
  facts) — previously this was 1 large call, 70B, whose own prose WAS the
  reply.
- Roman Urdu / Urdu, billing/document intent: **1 call, 70B** (JSON step,
  needs the reasoning) **+ 1 small call, 70B** (writer step) — same call
  count as before, but the writer call's prompt is tiny (owner's message +
  a one-line fact summary) instead of the full system prompt.
- Roman Urdu / Urdu, agent layer auto-executes (e.g. "send Ali his invoice"
  resolves fully): **1 call** (JSON step only) — **the writer step is
  skipped**, because `apply_safe_document_send()` already produced the final,
  correctly-localized text via a Python template, not a model call.
- **Every** call to `call_groq_text()` (`apps/chat/groq_client.py:70-92`)
  still hardcodes `reasoning=True` — this covers
  `transliterate_to_roman_urdu()` (dictated Urdu-script input → Roman Urdu,
  before the message even reaches the model as chat input).

So a Roman Urdu voice message can still trigger up to **3 Groq calls**
(transliterate-in, JSON step, writer step) in the worst case, same as
before — but many ordinary turns now need only 1-2, the JSON step usually
runs on 8B instead of 70B, and the writer step's prompt is a couple of short
lines instead of the full system prompt. See `AGENT_DATA_FLOW.md` §8 for the
full before/after call table.

Also relevant: `apps/image_info_extractor/gemini_client.py` uses **Google
Gemini**, not Groq at all, and only for OCR receipt/photo extraction — it's
kept off Groq's flow entirely and is unrelated to the fast/reasoning choice
above (comment in `select_model_tier`'s docstring explains Gemini's free
tier is too low-quota — 20 req/day — for ordinary chat volume).

## 6.6. API key rotation, reliability fix, and 20-key support

Both Groq and Gemini support rotating across multiple API keys
(`GROQ_API_KEY`, `GROQ_API_KEY_1`..`GROQ_API_KEY_20`, and the equivalent
`GEMINI_API_KEY*` vars), read by `_collect_keys()` in
`accountant_backend/settings.py:177-193` and now capped at **20** keys each
(raised from 10 — just a `max_n=20` argument at the two call sites, nothing
else hardcodes the count).

**A real production bug was found and fixed here.** The symptom: when the
active Groq key ran out of quota, the owner sometimes saw a raw error
immediately — rotation only appeared to "kick in" on their *next* message.
The rotation loop in `apps/chat/groq_client.py: call_groq()` was always
correct (it does try every configured key before raising) — the bug was
**speed**, not logic: the underlying `groq` SDK client retries a failed
request internally, with backoff, *before* it ever raises an exception back
to our loop (2 extra attempts per key by default). With N keys, one failed
chat request could turn into N × (1 + SDK retries) attempts with backoff
sleeps between them — easily long enough to blow through the request's own
timeout, so the owner saw a failure even though a later key in the list was
perfectly healthy. The fix (`groq_client.py: _client_for()`) constructs
every key's client with `Groq(api_key=key, max_retries=0)`, so each key
attempt fails once, fast, and our own loop is the only retry authority.

**The user-facing contract now is:** the owner sees an error only if *every
configured key* fails. If you ever suspect a key is being skipped or the
rotation is slow again, `_client_for`'s comment in `groq_client.py` is the
first place to look, and `max_retries=0` is the specific thing to check
hasn't been reverted.

## 7. Diagram

```
 Owner's WhatsApp/chat message
            │
            ▼
 ┌──────────────────────────────┐
 │ apps/chat/services.py        │
 │   generate_reply()           │──── builds prompt from ──▶ ChatMessage history (DB)
 │                               │                            + live business data (DB)
 └──────────────┬────────────────┘
                │ messages[]
                ▼
 ┌──────────────────────────────┐
 │ apps/chat/groq_client.py     │   JSON/INTENT STEP — 8B or 70B by
 │   call_groq(reasoning=...)   │   intent complexity only (needs_reasoning),
 │   (Groq API, OpenAI-compat)  │   NEVER by language. NOT function/tool calling.
 └──────────────┬────────────────┘
                │ JSON: {text, draft_document, draft_bill, draft_action, ...}
                │ ("text" here is a placeholder for ur/roman_ur — see below)
                ▼
 ┌──────────────────────────────┐
 │ apps/chat/services.py        │   ChatMessage row created (needed for the
 │   ChatMessage.objects.create │   AgentGoal FK below) — text is provisional
 └──────────────┬────────────────┘   for ur/roman_ur until the writer step runs
                │ reply_data, ai_message
                ▼
 ┌──────────────────────────────┐
 │ apps/chat/services.py        │
 │   apply_safe_document_send() │──── returns True if it already wrote
 │                               │     final, localized text (canned
 └──────────────┬────────────────┘     template or Clarification) — writer
                │ reply_data                        step is then skipped
                ▼
 ┌──────────────────────────────┐        ┌─────────────────────────────┐
 │ apps/agent/planner.py        │  reads │ apps/agent/capabilities.py  │
 │   plan_from_reply()          │◀──────▶│   CAPABILITIES = {...}      │
 │   backward-chains from goal  │        │   (the only "tools" that    │
 │   to an ordered step list    │        │    exist — LLM never sees   │
 └──────────────┬────────────────┘        │    this registry)          │
                │ steps[]                 └─────────────────────────────┘
                ▼
 ┌──────────────────────────────┐
 │ apps/agent/executor.py       │
 │   execute_plan()             │──── writes progress to ──▶ AgentGoal (DB)
 │   runs each step.execute()   │      via GoalManager
 │   stops on failure/async     │
 └──────────────┬────────────────┘
                │ (if async, e.g. WhatsApp send)
                ▼
 ┌──────────────────────────────┐
 │ background job worker         │
 │ (apps/documents/delivery.py)  │──── on completion calls ──▶ GoalManager.handle_event()
 └──────────────┬─────────────────┘     which closes out the AgentGoal
                │
                ▼   (back in generate_reply, only if apply_safe_document_send
                │    did NOT already write final text, AND language is ur/roman_ur)
 ┌──────────────────────────────┐
 │ apps/chat/services.py        │   RESPONSE-WRITER STEP — 70B always.
 │   _build_execution_summary() │   Input: owner's message + a plain fact
 │   _write_final_reply()       │   summary (draft_action/document.summary,
 │   (call_groq, reasoning=True)│   or a built draft_bill description) —
 └──────────────┬────────────────┘   NEVER the JSON step's own "text".
                │ composed text + speech_text
                ▼
      ai_message.text/speech_text saved — this is what the owner sees
```

## 8. Honest assessment — can a 2nd-year student maintain this?

**Yes, more easily than a LangChain/LangGraph version would be**, with some
caveats.

**Why it's actually approachable:**
- Every file has a clear, single job (prompt building / LLM call / planning /
  execution / state tracking). No hidden framework magic, no "what does
  `AgentExecutor.invoke()` actually do internally" black box.
- You can read `capabilities.py` top to bottom and know the entire set of
  things the AI can cause to happen. That's the whole attack surface, in one
  file, ~360 lines.
- Adding a `print()` or a breakpoint anywhere in this chain works exactly
  like it would in any Django code, because it *is* just Django code.

**What's genuinely clever (and therefore worth respecting, not just
"fragile"):**
- `planner.py`'s backward-chaining (`compose_plan`) is a real, if small,
  planning algorithm — it's not just a big if/elif chain. It's maybe 25 lines
  and worth understanding fully (re-read section 3, step 5) before touching
  it, because a wrong `required_inputs`/`outputs` set on a capability could
  make the planner silently pick the wrong chain or throw `PlanningError`.
- The retry/fallback logic around Roman Urdu script leakage
  (`apps/chat/services.py`) encodes real, hard-won behavior about a specific
  small model's failure mode. Don't simplify it away without understanding
  why it's there — the comments explain real production incidents.

**Risky to touch without care:**
1. **`apps/agent/capabilities.py`** — this is the safety boundary. Adding a
   new capability here means the LLM can now indirectly cause that action.
   Get the `required_inputs`/`outputs` sets exactly right, and think hard
   about `risk_tier` before adding anything that writes money data.
2. **`apps/chat/prompt.py`'s `OUTPUT_CONTRACT_INSTRUCTIONS`** — this is a
   long, carefully-tuned prompt. Groq's smaller models are not that reliable
   at following JSON schemas, so tiny wording changes can break the JSON
   shape the rest of the pipeline assumes. Change it in small steps and test.
3. **`GoalManager`** (`apps/agent/goals.py`) — the docstring says it plainly:
   this is "the only write path to `AgentGoal`." If you write directly to an
   `AgentGoal` from somewhere else, you'll break the audit trail and possibly
   the async handoff (a WhatsApp delivery confirming into a goal that's not
   in `awaiting_verification` status silently does nothing —
   see `handle_event`).

**Process for adding a new agent-driven action (e.g. "let the AI apply a
discount"):**
1. Write the plain Django service function that does the actual work (in
   `apps/sales/services.py` or similar) — same as any normal feature.
2. Add a `Capability` entry in `apps/agent/capabilities.py` wrapping it, with
   correct `required_inputs`/`outputs`/`risk_tier`.
3. If it should be reachable from a `draft_document`/`draft_action` JSON
   field the model already emits, wire it into `planner.py`'s
   `_GENERATOR_FOR_DOC_TYPE`-style mapping (or the tap-confirm flow, which
   this new-capability system deliberately doesn't yet replace for financial
   actions — see `capabilities.py`'s "Financial tier" comment block).
4. If it's a genuinely new *intent*, you also need to teach the model to emit
   it: add a new JSON field/shape to `OUTPUT_CONTRACT_INSTRUCTIONS` in
   `prompt.py`, and make sure `apps/chat/serializers.py`'s `AiReplySerializer`
   accepts it.

This is a few coordinated small edits across 2-3 files, not a rewrite — it's
tedious in the sense that you touch several files, but each edit is small and
the pattern is consistent. That's very manageable for one person.

## 9. Should this migrate to LangGraph?

**No — don't migrate. It would make this harder to maintain for you, not
easier.**

Reasons:
- **LangGraph adds a graph/node/edge abstraction on top of code that is
  already, in effect, a graph** (the planner's backward chaining *is* your
  DAG). You'd be learning LangGraph's state channels, node functions,
  conditional edges, and checkpointing model — a whole new mental model — to
  re-express something that's currently just... Python functions calling
  Python functions. That's strictly more to learn, not less.
- **The actual hard part of this system isn't orchestration — it's the
  domain logic**: what counts as "safe" vs "financial" risk, how business
  dates resolve, how WhatsApp session status gets checked, Roman Urdu script
  handling. None of that gets easier or safer by wrapping it in LangGraph
  nodes. You'd still need to understand all of it.
- **This system already has the two things LangGraph exists to give you**:
  explicit state (the `AgentGoal.plan` JSONField) and structured control flow
  (planner → executor). You'd be paying migration risk to get something
  you already have, in a form you already understand.
- **The safety guarantee in section 4** — "only capabilities in this dict are
  reachable" — is easiest to reason about exactly because it's a plain
  Python dict, not because it's inside any particular framework. A LangGraph
  tool-node setup wouldn't inherently be safer; it would just be the same
  idea, in unfamiliar syntax, at exactly the moment you can least afford
  unfamiliar syntax (right before your Claude access ends).

**If you want a simpler refactor instead**, the lowest-risk, highest-value
thing to do is *not* touching the framework at all, but writing small tests
around `apps/agent/planner.py` and `apps/agent/executor.py` (they're pure
enough to unit test without hitting Groq) so you have a safety net before you
start extending `capabilities.py` on your own. That buys you far more actual
maintainability than any framework swap would.
