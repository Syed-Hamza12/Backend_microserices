# Agent System — Component & Data-Flow Reference

Companion to `AGENTS.md` (read that first for the narrative explanation).
This file is a debugging/extension reference: exact request/response shapes
at every hop, which file to open for which symptom, and the step-by-step
recipe for wiring in a new tool.

## 1. Component map

```
apps/chat/                          apps/agent/
┌─────────────────┐                ┌──────────────────┐
│ views.py         │  HTTP in       │ capabilities.py   │  tool registry
│ services.py      │  orchestrates  │ planner.py         │  decides WHAT
│ prompt.py        │  builds prompt │ executor.py        │  decides HOW
│ google_client.py │  calls Gemini  │ goals.py           │  state machine
│ serializers.py   │  validates out │ models.py (AgentGoal)│ persistence
│ models.py         │  ChatMessage  │ results.py          │ Outcome/Clarification
└─────────────────┘                │ recovery.py         │ opt-in retry
                                    └──────────────────┘

apps/integrations/
┌───────────────────────────┐
│ google_genai_client.py     │  canonical Google API client:
│                             │  key rotation + per-key quota
│                             │  cooldown + model fallback,
│                             │  shared by every Google caller
└───────────────────────────┘

apps/image_info_extractor/
┌───────────────────────────┐
│ gemini_client.py           │  vision OCR extraction (Gemini)
│ clarification.py           │  OCR clarification wording
│                             │  (Gemma fast / Gemini reasoning)
└───────────────────────────┘
```

Everything below is one continuous call chain across these apps.
**`apps/chat/groq_client.py` no longer exists.** Every model call in this
codebase — chat's fast tier, chat's reasoning tier, the ur/roman_ur
response-writer step, transliteration, Urdu-script TTS conversion, OCR
extraction, and OCR clarification wording — goes through Google's
Generative Language API via `apps/integrations/google_genai_client.py`.

## 2. Full call chain, with exact function signatures

```
1. HTTP request  → apps/chat/views.py
        │  (calls) generate_reply(business, conversation, text, language)
        ▼
2. apps/chat/services.py :: generate_reply()
        │
        ├─▶ _recent_history(conversation, limit)         [reads ChatMessage rows]
        ├─▶ prompt.build_messages(business, history, text, language)
        │        │
        │        ├─▶ build_system_prompt(business, language)
        │        │        ├─▶ OUTPUT_CONTRACT_INSTRUCTIONS   (static string, the "tool schema")
        │        │        ├─▶ build_business_context(business)   [SQL: customers, balances]
        │        │        └─▶ build_entry_context(business)      [SQL: recent entries]
        │        └─▶ returns messages = [{"role": "system", ...}, {"role":"user"/"assistant",...}, ...]
        │
        ├─▶ select_model_tier(text, language)  -> "fast" | "reasoning"
        │        (intent-complexity only — needs_reasoning(text). Language does
        │         NOT force this anymore; see §8 for the full model-routing story)
        │
        ├─▶ _call_model(tier, messages)          ["JSON/INTENT STEP"]
        │        └─▶ tier == "fast"       → google_client.call_gemma_planner(messages=messages)
        │            tier == "reasoning"  → google_client.call_gemini_reasoning(messages=messages)
        │                 │
        │                 └─▶ apps/integrations/google_genai_client.py :: generate(keys, models, contents, config=...)
        │                          tries each model in the ladder, each non-cooled-down key in turn
        │                 returns: raw JSON string
        │
        ├─▶ _parse_and_validate(raw)   -> dict "reply_data"
        │        shape: {
        │          "text": str,        ← for ur/roman_ur this is a PLACEHOLDER,
        │                                 discarded/overwritten below — never
        │                                 shown to the owner (see step 5)
        │          "speech_text": str | "",
        │          "draft_bill": {...} | null,
        │          "draft_action": {...} | null,   (has its own "summary": str)
        │          "draft_document": {"doc_type": "invoice"|"receipt"|"statement"|"report",
        │                              "customer_id"?: int, "customer_name"?: str,
        │                              "date_from"?: str, "date_to"?: str, "format"?: str,
        │                              "summary": str} | null,
        │          "document_ready": {...} | null
        │        }
        │        (validated via apps/chat/serializers.py :: AiReplySerializer)
        │
        ├─▶ ChatMessage.objects.create(...)   [persists the owner msg + the AI msg;
        │        text/speech_text here are provisional for ur/roman_ur]
        │
        ├─▶ apply_safe_document_send(business, conversation, ai_message, reply_data, language)
        │        returns bool: True if it already wrote FINAL localized text
        │        (a Python-template outcome string or a Clarification — see below)
        │        │
        │        ├─▶ apps/agent/planner.py :: plan_from_reply(business, conversation, ai_message, reply_data)
        │        │        reads reply_data["draft_document"] ONLY — draft_bill/draft_action
        │        │        never reach the agent app (see §5, "what's NOT wired in yet")
        │        │        │
        │        │        ├─▶ compose_plan(generator_capability, have)   [backward-chains]
        │        │        │        loop: for each missing required_input, find_producer() in CAPABILITIES
        │        │        │        returns step_names: list[str], e.g.
        │        │        │        ["find_customer","find_latest_entry","choose_rendering_format",
        │        │        │         "generate_document_from_entry","send_whatsapp_document"]
        │        │        │
        │        │        └─▶ for each step_name: CAPABILITIES[name].resolve(business, conversation, have)
        │        │                 returns dict (merged into `have` for the next step) OR Clarification
        │        │        returns: list[{"capability": str, "resolved": dict}]  OR  Clarification  OR  None
        │        │
        │        └─▶ apps/agent/executor.py :: execute_plan(business, conversation, message, steps)
        │                 │
        │                 ├─▶ GoalManager.start(...)              [creates AgentGoal row, status="executing"]
        │                 │
        │                 ├─▶ for each step: CAPABILITIES[name].execute(business, resolved)
        │                 │        returns Outcome(success, output, text, pending_delivery_id, waiting_on)
        │                 │        (outcome.text is a Python template string, e.g.
        │                 │         apps/agent/capabilities.py's _SENDING_TEXT — NOT model output)
        │                 │
        │                 ├─▶ GoalManager.record_step(goal, index, outcome)   [updates AgentGoal.plan JSONField]
        │                 │
        │                 └─▶ GoalManager.advance(goal, status=...)
        │                          status ∈ {"failed", "awaiting_verification", "done"}
        │
        └─▶ [ONLY if language in ("ur","roman_ur") AND NOT ai_failed AND
             apply_safe_document_send returned False — i.e. nothing above
             already wrote final localized text]     ["RESPONSE-WRITER STEP"]
                 │
                 ├─▶ _build_execution_summary(reply_data)  -> plain-text summary
                 │        draft_action.summary, or draft_document.summary, or a
                 │        built draft_bill description, or (no draft at all)
                 │        reply_data["text"] used as raw CONTENT, not shown directly
                 │
                 └─▶ _write_final_reply(original_user_text, summary, language)
                          └─▶ google_client.call_gemini_reasoning(messages=[...]) with a SHORT prompt:
                                 owner's message + execution summary ONLY —
                                 no system prompt, no business context, no history
                              returns: (composed_text, composed_speech_text)
                          ai_message.text/speech_text saved with this — THIS is
                          what the owner actually sees for ur/roman_ur turns

   ── if a step set outcome.waiting_on (e.g. send_whatsapp_document queued a job) ──

3. apps/documents/delivery.py :: handle_document_send_job()   [background job, separate process/request]
        │  on success or failure of the actual WhatsApp send:
        └─▶ apps/agent/goals.py :: GoalManager.handle_event(event_type, payload)
                 finds AgentGoal by delivery_id + status="awaiting_verification"
                 sets goal.status = "done" | "failed"
                 (the chat app's ChatMessage.pending_delivery lets the client poll
                  DocumentDelivery status independently — see §4)
```

## 3. Data shapes at each boundary (for debugging)

| Boundary | Shape | Where defined |
|---|---|---|
| LLM output (raw) | JSON string | Gemini/Gemma response, forced by `response_mime_type="application/json"` (`response_format_json=True`) |
| LLM output (parsed) | `reply_data: dict` | `apps/chat/serializers.py: AiReplySerializer` |
| planner input | `reply_data["draft_document"]: dict` | `apps/agent/planner.py: plan_from_reply()` |
| planner `have` dict | plain scalars + `_customer`/`_entry` object refs | built up across `resolve()` calls |
| planner output | `list[{"capability": str, "resolved": dict}]` | `apps/agent/planner.py` |
| executor→capability | `capability.execute(business, resolved: dict)` | `apps/agent/capabilities.py` |
| capability output | `Outcome(success, output, text, pending_delivery_id, waiting_on)` | `apps/agent/results.py` |
| persisted goal state | `AgentGoal.plan: list[{"capability","status","output"}]` | `apps/agent/models.py` (JSONField) |
| execution summary (ur/roman_ur only) | plain string, e.g. `"Prepared a statement for Ali, 1 Jul to 31 Jul."` | `apps/chat/services.py: _build_execution_summary()` |
| response-writer input | `f"OWNER'S MESSAGE:\n{text}\n\nEXECUTION SUMMARY:\n{summary}"` | `apps/chat/services.py: _write_final_reply()` |
| response-writer output | `(text: str, speech_text: str \| None)` | same |
| what the client sees | `ChatMessage.text` (agent-layer canned text, OR response-writer output, OR the JSON step's own text for English) + `ChatMessage.pending_delivery` (FK, polled) | `apps/chat/models.py` |

**Key thing to know for debugging**: `have`/`resolved` dicts carry both
JSON-safe scalars (`customer_id`, `amount`) AND live Django model instances
under underscore-prefixed keys (`_customer`, `_entry`) so later steps don't
need to re-query the DB. `GoalManager._json_safe()`
(`apps/agent/goals.py:10-25`) strips every `_`-prefixed key before writing to
the DB — so if you add a new capability that stashes something under a
non-underscore key that isn't JSON-serializable, `goal.save()` will throw.
Prefix any object reference with `_`.

## 4. Where to look for a given symptom

| Symptom | Look here |
|---|---|
| Model returns malformed/unexpected JSON | `apps/chat/prompt.py` — `OUTPUT_CONTRACT_INSTRUCTIONS`; check `_parse_and_validate` in `services.py` for what "bad shape" triggers a retry |
| Model picks the wrong model tier (Urdu garbled, or slow) | `apps/chat/services.py: select_model_tier()` |
| Agent never triggers for a document request | `apps/agent/planner.py: _GENERATOR_FOR_DOC_TYPE` — doc_type must be one of `invoice`/`receipt`/`statement`; `report` intentionally falls through untouched |
| Agent says "I couldn't find/do X" when it should work | A capability's `resolve()` returned a `Clarification` — add logging in `apps/agent/capabilities.py` at the specific `_resolve_*` function |
| A step ran but had no visible effect | Check `AgentGoal.plan` for that goal row in Django admin/shell — `AgentGoal.objects.filter(conversation=...).first().plan` |
| WhatsApp send queued but never confirms | `apps/documents/delivery.py: handle_document_send_job` → `GoalManager.handle_event`; check `AgentGoal.status` stuck at `awaiting_verification` and `DocumentDelivery` status directly |
| New capability crashes the whole chat turn | It shouldn't — `execute_plan()` wraps every `capability.execute()` call in `try/except Exception` (`apps/agent/executor.py:47-54`) and turns it into a failed `Outcome` instead of a 500. If chat is 500ing, the bug is upstream (planner/resolve, or capabilities.py import error) |
| Planner throws `PlanningError` | A capability's `required_inputs`/`outputs` sets don't chain to the target — likely a typo in a set literal in `capabilities.py`, or a missing capability. `PlanningError` is caught in `plan_from_reply` and silently falls through to `None` (existing tap-confirm flow), so check logs, not user-facing errors |
| Every reply is suddenly the fallback text ("Sorry, I couldn't process that…") | Almost certainly every configured Gemini key is on cooldown at once — check `apps/integrations/google_genai_client.py`'s in-process `_cooldowns` dict (or just wait 60s and retry) before assuming a real outage. See §8.5. |
| A key that should be healthy keeps getting skipped | It's on cooldown from a prior 429/RESOURCE_EXHAUSTED — cooldowns are per-process and in-memory, so restarting the process also clears them (not a fix, just a fact to know while debugging) |

## 5. What's NOT wired into the agent app (deliberately)

- `draft_bill` and `draft_action` never reach `apps/agent/`. They still go
  through the older tap-confirm flow (`record_drafted_bill`,
  `ConfirmDraftBillView`/`ConfirmDraftActionView` — search those names in
  `apps/chat/services.py` and `apps/chat/views.py`). Only `draft_document`
  for `invoice`/`receipt`/`statement` is auto-composed through the
  planner/executor today. This was a deliberate scope limit (see
  `capabilities.py`'s "Financial tier" comment) — don't assume
  `record_payment` is reachable from a chat message yet; it exists in the
  registry but nothing currently calls `compose_plan` targeting it from
  `plan_from_reply`.
- `report`-type documents (whole-business, no single customer) are not
  auto-composed — `_GENERATOR_FOR_DOC_TYPE` in `planner.py` has no entry for
  `"report"`, so those still go through the old flow untouched.

## 6. Recipe: adding a new tool/capability end-to-end

Concrete steps, in order, with the exact file each touches:

1. **Write the real logic** as a normal Django service function, e.g. in
   `apps/sales/services.py`. No agent-specific code here — just the feature.

2. **Register it as a capability** in `apps/agent/capabilities.py`:
   ```python
   def _resolve_my_new_thing(business, conversation, have):
       # read-only validation / lookups. Return a dict of new keys on success,
       # or Clarification("a question for the owner") if something's missing/ambiguous.
       ...

   def _execute_my_new_thing(business, resolved):
       # the actual side effect. Return Outcome(success=True, output={...}, text="...")
       ...

   CAPABILITIES["my_new_thing"] = Capability(
       name="my_new_thing",
       risk_tier="safe",  # or "financial" / "dangerous" — think about this
       required_inputs={"customer_id"},   # what must already be in `have`
       outputs={"my_output_key"},          # what this adds to `have` for later steps
       side_effects=True,                  # False only for pure reads
       synchronous=True,                   # False if it queues a background job (see send_whatsapp_document)
       resolve=_resolve_my_new_thing,
       execute=_execute_my_new_thing,
   )
   ```

3. **Decide how it gets triggered**:
   - If it should chain automatically off an existing `draft_document` type,
     add/extend an entry in `apps/agent/planner.py: _GENERATOR_FOR_DOC_TYPE`.
   - If it needs a wholly new LLM-expressible intent, you need step 4 below.
   - If it's meant to be called directly (not LLM-triggered), you don't need
     the planner at all — just call `CAPABILITIES["my_new_thing"].execute()`
     from a view, same as any function call.

4. **If it's a new intent the model must express**, extend the prompt
   contract in `apps/chat/prompt.py` (`OUTPUT_CONTRACT_INSTRUCTIONS`) with
   the new JSON field/shape, and extend
   `apps/chat/serializers.py: AiReplySerializer` (and whichever
   `DraftXSerializer` matches) to accept and validate it. Test by sending a
   real message and checking `reply_data` in a debugger/log before it even
   reaches the agent app — isolate "does the model emit the right JSON" from
   "does the planner correctly consume it."

5. **Test the planner/executor without hitting Gemini**: since `resolve`/
   `execute` are plain functions, you can unit-test
   `compose_plan("my_new_thing", have)` and
   `execute_plan(business=..., conversation=..., message=..., steps=[...])`
   directly with a fake `have` dict and a real (test) database — no LLM call
   needed. This is the fastest debug loop; use it before testing through chat.

6. **If risk_tier is "financial" or "dangerous"**, do not wire it to
   auto-execute from `plan_from_reply`/`apply_safe_document_send` the way
   `send_whatsapp_document` does. Route it through a tap-confirm pattern
   instead (owner must explicitly confirm before `execute()` runs) — follow
   the existing `ConfirmDraftBillView`/`ConfirmDraftActionView` pattern in
   `apps/chat/views.py` rather than the auto-composed path. This is exactly
   why `record_payment` (financial tier) sits in the registry unused by the
   planner today — treat that as the template to copy, not an oversight to
   "fix" by wiring it in casually.

## 8. Model consumption map — exactly which model runs, per call site

**Updated**: chat is now fully off Groq. Both tiers run on Google, through
two thin wrapper functions in `apps/chat/google_client.py`, both of which
delegate rotation/fallback/cooldown to the shared
`apps/integrations/google_genai_client.py`:

```
GOOGLE_FAST_MODEL     = gemma-4-31b-it              ("fast" tier — cheap, quick)
GOOGLE_QUALITY_MODEL  = gemini-3.5-flash-lite       ("reasoning" tier — was Groq's 70B)
```

configured in `accountant_backend/settings.py` (`GOOGLE_FAST_MODEL` and
`GOOGLE_QUALITY_MODEL`, near the `GEMINI_*` block).

The two tiers are still reached through **two separate wrapper functions** —
kept distinct so each can point at its own key pool and model ladder, even
though both now flow through the same underlying `generate()`:
- `apps/chat/google_client.py: call_gemma_planner(messages)` — fast tier,
  uses `settings.FAST_GEMINI_API_KEYS` / `GOOGLE_FAST_MODEL`.
- `apps/chat/google_client.py: call_gemini_reasoning(messages, response_format_json=True)`
  — reasoning tier, uses `settings.QUALITY_GEMINI_API_KEYS` / `GOOGLE_QUALITY_MODEL`.
  Also used for the ur/roman_ur response-writer step (`response_format_json`
  varies by language — see the table below).
- `apps/chat/google_client.py: call_gemini_text(instructions, text)` — thin
  wrapper over `call_gemini_reasoning` with `response_format_json=False`,
  for transliteration and Urdu-script conversion (was `call_groq_text`).

`apps/chat/services.py: _call_model(tier, messages)` is still the single
dispatch point between the two tiers — every consumption question reduces
to "which tier did `select_model_tier()` pick," same as before.

**A chat turn is up to two separate Gemini reasoning-tier calls with two
separate jobs** (unchanged from the Groq-era design, only the provider
moved):

1. **JSON/intent step** — understands the message, produces
   draft_bill/draft_action/draft_document per the full output contract, plus
   a `"text"` field. Tier picked by `select_model_tier()`, purely on intent
   complexity. **For `ur`/`roman_ur`, this `"text"` is a throwaway
   placeholder — it is never shown to the owner.**
2. **Response-writer step** — `ur`/`roman_ur` only, and ONLY when nothing
   else already produced final localized text (see the skip conditions
   below). Composes a **brand-new** reply from the owner's original message
   plus a plain execution summary — it is **not** given the JSON step's
   `"text"` and is not asked to rewrite anything. Always the reasoning tier.

| Call site | File : line | Model used | Condition |
|---|---|---|---|
| JSON/intent step (all languages) | `services.py: generate_reply()` → `_call_model(tier, ...)` | Gemma **or** Gemini quality model | `tier = select_model_tier(text, language)` — **language no longer forces this**, only `needs_reasoning(text)` does; `tier == "fast"` → `google_client.call_gemma_planner()`, `tier == "reasoning"` → `google_client.call_gemini_reasoning()` |
| Response-writer step | `services.py: _write_final_reply()` | **Gemini quality model always**, only when `language in ("ur","roman_ur")` AND `apply_safe_document_send()` did NOT already write final text | Short prompt = owner's message + `_build_execution_summary()`'s output — never the JSON step's own `"text"`, never the full context |
| Roman-Urdu-in transliteration | `services.py: transliterate_to_roman_urdu()` → `call_gemini_text()` | **Gemini quality model always** | `call_gemini_text()` always runs through `call_gemini_reasoning` — no fast path exists |
| Urdu-script TTS conversion | `services.py: to_urdu_script()` → `call_gemini_text()` | **Gemini quality model always** | same as above |
| Receipt/photo OCR | `apps/image_info_extractor/gemini_client.py` | **Gemini** (its own dedicated OCR model/key pool, `GEMINI_API_KEYS`/`GEMINI_MODEL`) | always — separate prompt/schema, unrelated to fast/reasoning choice, and NOT wired through `google_genai_client.py`'s cooldown logic unless that file was separately updated (check before assuming it has cooldown behavior) |
| OCR clarification follow-up wording | `apps/image_info_extractor/clarification.py` → `call_gemma_planner()` / `call_gemini_reasoning()` | Gemma **or** Gemini quality model | its own narrow two-tier split (`len(missing_fields) > 1` picks reasoning), outside `select_model_tier()`, but now shares the **same key pools** as chat's fast/reasoning tiers — see the shared-quota caveat below |

`select_model_tier()` rule (`apps/chat/services.py`) — unchanged, same rule
for every language:

```python
def select_model_tier(message_text, language):
    return "reasoning" if prompt.needs_reasoning(message_text) else "fast"
```

Language-based routing still lives in the response-writer step, which runs
**only** for `ur`/`roman_ur`, **only** when nothing already wrote final
text, and **composes fresh** rather than rewriting the JSON step:

```python
_WRITER_LANGUAGES = ("ur", "roman_ur")
agent_overwrote_text = apply_safe_document_send(...)  # True = already final, localized
if language in _WRITER_LANGUAGES and not ai_failed and not agent_overwrote_text:
    summary = _build_execution_summary(reply_data)     # NOT reply_data["text"]
    final_text, final_speech = _write_final_reply(text, summary, language)
```

**Three ways a `ur`/`roman_ur` turn can end, each with a different call
count** — this is the part worth understanding for cost tuning:

| Scenario | JSON-step model | Response-writer call? | Why |
|---|---|---|---|
| Plain question/small talk, no draft | **Gemma (fast)** | **Yes, quality model, tiny prompt** | No structured summary to draw from besides re-expressing the JSON step's raw `"text"` as content — still composed fresh, not rewritten |
| draft_bill / draft_action / unsupported draft_document (e.g. "report") | Gemma or quality model (by `needs_reasoning`) | **Yes, quality model, tiny prompt** | `apply_safe_document_send()` returns `False` (`plan is None`) — nothing else wrote final text |
| draft_document the agent layer fully auto-executes (invoice/receipt/statement `send`) | Gemma or quality model (by `needs_reasoning`) | **No — skipped entirely** | `apply_safe_document_send()` already wrote final, localized text via a **Python template string** (`apps/agent/capabilities.py`'s `_SENDING_TEXT`) or a `Clarification` message — not a model call at all |

**Worst case for call count**: a dictated Roman-Urdu voice message with
billing intent, no agent auto-execution → transliterate-in (quality) + JSON
step (quality) + response-writer (quality) = **3 calls touching the
quality/reasoning model** — same count as before this refactor, but the
response-writer's prompt is two short paragraphs (owner's message + one-line
summary), not the full system prompt.
**Best case for `ur`/`roman_ur`**: a "send Ali his invoice" that the agent
layer fully auto-executes → **1 call total** (JSON step only, response-writer
skipped).
**Best case overall** (unchanged): a typed English question with no
billing/document intent → **1 call to the fast (Gemma) model, nothing else.**

**Where to intervene if you need to cut consumption further:**
- `prompt.needs_reasoning()` (in `apps/chat/prompt.py`) is the regex/intent
  check deciding fast-vs-reasoning for the JSON step, for every language now
  — tightening or loosening this directly shifts traffic between the two
  models, language-independently.
- The response-writer step is unconditionally the quality model for
  `ur`/`roman_ur` by design (this is the one place actual write-quality is
  enforced) — re-read `select_model_tier()`'s docstring before changing
  this; it documents the real production incident (garbled Urdu shown to an
  owner) this exists to prevent.
- `_build_execution_summary()`/`_write_final_reply()`'s prompt is already
  minimal — owner's message + a one-line fact summary, never the full
  context. Don't accidentally widen it by passing business context or chat
  history "for better phrasing"; that reintroduces the cost this refactor
  removed.
- If you ever add a new capability whose outcome should be spoken aloud
  automatically (like `send_whatsapp_document`'s `_SENDING_TEXT`), write it
  as a plain per-language Python string dict, the same pattern — that skips
  the response-writer call entirely for that turn.
- `MAX_TRANSLITERATE_CHARS` (`services.py`) caps how much text goes into the
  transliteration call — lowering it reduces tokens-per-call but doesn't
  change which model runs.
- On a **free-tier Gemini plan**, the more urgent lever than any of the
  above is simply how many keys are in `QUALITY_GEMINI_API_KEYS` — see §8.5:
  each key gets its own independent per-minute quota, and cooldowns are
  per-key, so more keys is the most direct way to raise the request ceiling
  before you start caring about which tier a given message is routed to.

## 8.5. Gemini key rotation + per-key quota cooldown

Groq is gone entirely from this codebase — `apps/chat/groq_client.py` no
longer exists, and there is nothing left to fall back to if
`apps/integrations/google_genai_client.py`'s Google calls fail across every
key.

`_collect_keys()` (`accountant_backend/settings.py`) is still the single
config-driven key loader, `max_n=20` for every pool. The chat reasoning
tier's pool is loaded from `GEMNI_REASONING_KEY` / `GEMNI_REASONING_KEY_1` ..
`GEMNI_REASONING_KEY_20` (falls back to `GEMINI_API_KEYS` if none of those
are set) into `settings.QUALITY_GEMINI_API_KEYS`.

**Quota cooldown (new)**: `apps/integrations/google_genai_client.py` now
tracks, per API key, a cooldown timestamp in an in-process dict
(`_cooldowns`). Whenever a call against a key fails with a quota/rate-limit
error (429 / `RESOURCE_EXHAUSTED` — checked via `_is_quota_error()`), that
key is marked unavailable for `COOLDOWN_SECONDS` (60s, matching Gemini
free-tier per-minute quota windows). `generate()`'s rotation loop skips any
key currently on cooldown without attempting it, and the cooldown entry is
lazily deleted the moment it's checked after expiring — no background
cleanup task needed. A `503` (model busy) or any other non-quota error does
**not** cool a key down; only a quota error does, so a single transient
network blip can't take a healthy key out of rotation for a minute.

This directly targets the free-tier failure mode this change was made for:
without it, a key that just hit its per-minute quota would be retried on
the very next request anyway, fail again, and only "recover" once its
quota reset on its own — the old Groq-era design's `max_retries=0` fix
solved a *retry-storm-inside-one-request* problem, but did nothing for
*repeatedly re-trying an already-known-exhausted key across requests*.
Cooldown solves that second problem directly: once a key 429s, nothing
tries it again until the cooldown clock says the quota window has likely
reset.

Ordering is still **model-first, then key-within-model** (unchanged
reasoning: a 503 means the model is busy, not that any particular key is
bad, so trying a different model recovers faster than burning through every
key against a struggling one). Cooldown is layered on top of that ordering,
not a replacement for it — for a given model, cooled-down keys are simply
skipped as the key loop runs.

**Known limitations, worth being explicit about:**
- `_cooldowns` is a plain in-process dict — it resets on process restart
  and is **not shared across multiple worker processes**. Same caveat as
  the DRF throttle cache note in `settings.py`: fine for a single-process
  deployment, needs to move to Redis (or similar) if you ever run multiple
  gunicorn/uwsgi workers, or cooldown state won't be consistent across them.
- Cooldown duration is a flat 60s constant (`COOLDOWN_SECONDS`), not read
  from the actual `Retry-After` value Google may return — if Google's
  real per-minute window doesn't align exactly with 60s from the moment of
  failure, a key could still be retried slightly early (and just 429 again,
  harmlessly) or held back slightly longer than strictly necessary.
- `apps/image_info_extractor/gemini_client.py` (OCR extraction) was **not**
  migrated onto `google_genai_client.py`'s cooldown logic as part of this
  change — confirm directly in that file before assuming OCR calls get the
  same cooldown behavior.
- `apps/image_info_extractor/clarification.py` now shares
  `QUALITY_GEMINI_API_KEYS`/`FAST_GEMINI_API_KEYS` with chat's own JSON-step
  and response-writer calls — previously it had Groq's fully independent
  quota lane. A burst of OCR clarifications and a burst of chat reasoning
  traffic can now cool down keys the other one also needs. If this becomes
  a real contention problem, give `clarification.py` its own key pool
  (`CLARIFICATION_GEMINI_API_KEYS`, same `_collect_keys` pattern) rather
  than sharing.

## 9. Quick reference — files by responsibility

| File | Responsibility | Safe to edit freely? |
|---|---|---|
| `apps/chat/prompt.py` | System prompt / JSON contract the LLM must follow | Edit carefully, test with real Gemini calls after |
| `apps/chat/google_client.py` | Chat-specific Gemini wrappers: `call_gemma_planner`, `call_gemini_reasoning`, `call_gemini_text` | Edit when adding a new chat-tier call shape; keep the two tiers pointed at their own settings-defined key pools |
| `apps/integrations/google_genai_client.py` | Canonical Google API client — key rotation, quota cooldown, model fallback, shared by every Google-backed caller | Rarely needs touching; a fix here (e.g. cooldown duration, quota-error detection) affects every caller at once — treat changes carefully |
| `apps/chat/services.py` | Orchestrates one chat turn end-to-end | Central — read before editing anything else |
| `apps/chat/serializers.py` | Validates the LLM's JSON shape | Edit when adding a new JSON field to the contract |
| `apps/agent/capabilities.py` | The tool registry — safety boundary | Additive edits (new capabilities) are low-risk; editing existing ones needs care |
| `apps/agent/planner.py` | Backward-chaining composition | Edit `_GENERATOR_FOR_DOC_TYPE` freely; touch `compose_plan` itself rarely |
| `apps/agent/executor.py` | Runs steps in order | Rarely needs touching |
| `apps/agent/goals.py` | Sole writer of `AgentGoal` | Don't write to `AgentGoal` from anywhere else |
| `apps/agent/models.py` | `AgentGoal` schema | Migration required if changed |
| `apps/image_info_extractor/gemini_client.py` | Vision OCR extraction — its own Gemini model/key pool | Rarely needs touching; not on the shared cooldown path (see §8.5) unless separately migrated |
| `apps/image_info_extractor/clarification.py` | OCR clarification wording — now shares chat's fast/quality key pools | Edit if giving it its own key pool (see §8.5 caveat) |