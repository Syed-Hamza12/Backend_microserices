# How to Add a New Agent (Capability) to the Backend Workflow

> Companion to `AGENTS.md` and `AGENT_DATA_FLOW.md` at the repo root — those explain the *why*
> of the architecture. This file is a complete, verified, copy-paste-able step-by-step guide for
> the *how*, checked directly against the current source of:
> - `apps/agent/capabilities.py`
> - `apps/agent/planner.py`
> - `apps/agent/results.py`
> - `apps/agent/executor.py`
> - `apps/chat/prompt.py`, `apps/chat/serializers.py`, `apps/chat/views.py`

---

## 0. Mental model (read this first)

There is **no LangChain/AutoGPT-style agent framework** here. There is:

1. **One LLM call per chat turn** (Groq) that must return a fixed JSON contract
   (`apps/chat/prompt.py`).
2. A **capability registry** (`CAPABILITIES` dict in `apps/agent/capabilities.py`) — plain
   Python functions wrapped in a `Capability` dataclass. This is the "agent" in this codebase.
3. A **planner** (`apps/agent/planner.py`) that backward-chains from a target capability to a
   full ordered list of steps, by matching each capability's declared `required_inputs` against
   other capabilities' declared `outputs`.
4. An **executor** (`apps/agent/executor.py`) that runs the resolved plan step by step.

**The golden rule, stated in the codebase's own docstring:**
> A capability that doesn't exist in `CAPABILITIES` cannot be reached by the Planner, the
> Executor, or anything built on top of them — regardless of what the LLM outputs.

So adding a new capability is safe by construction as long as you follow the registration steps
below. Nothing auto-executes just because you wrote a function.

---

## 1. The `Capability` dataclass (exact definition)

From `apps/agent/capabilities.py` lines 37-49:

```python
RiskTier = Literal["safe", "financial", "dangerous"]

@dataclass
class Capability:
    name: str
    risk_tier: RiskTier                 # "safe" | "financial" | "dangerous"
    required_inputs: set                # keys that must already be in `have` before this can run
    outputs: set                        # keys this capability adds to `have` once it succeeds
    side_effects: bool                  # True if it writes to DB / calls an external API
    synchronous: bool                   # False if completion depends on an external event (e.g. WhatsApp delivery)
    resolve: Callable                   # (business, conversation, have) -> dict | Clarification
    execute: Callable                   # (business, resolved) -> Outcome
    verify: Optional[Callable] = None
    emits_events: frozenset = field(default_factory=frozenset)
    supported_doc_types: Optional[set] = None
```

There is no base class to subclass — you write two plain functions per capability:
`_resolve_<name>` and `_execute_<name>`.

### `resolve(business, conversation, have) -> dict | Clarification`
- `have` is a dict of everything already known/produced by earlier steps in the plan.
- Return a **plain dict** of newly-resolved values (should match/cover your declared `outputs`)
  if resolution succeeds.
- Return a **`Clarification`** (from `apps/agent/results.py`) if you cannot proceed — e.g.
  ambiguous customer, missing data, a precondition not met. Never guess.
- This is also where you re-validate against the real database (don't trust `have` blindly).

### `execute(business, resolved) -> Outcome`
- `resolved` is the dict your own `resolve()` returned.
- Do the actual work (DB write, external call, etc.) here, or just pass through if `resolve()`
  already did everything (see `generate_document_from_entry`/`_from_range`, which do all the
  real work in `resolve()` and are pure pass-throughs in `execute()`).
- Must return an `Outcome`.

### `Clarification` (exact definition, `apps/agent/results.py`)
```python
@dataclass
class Clarification:
    message: str            # shown verbatim to the owner as the reply's text
    code: str = "CLARIFICATION_NEEDED"
```

### `Outcome` (exact definition, `apps/agent/results.py`)
```python
@dataclass
class Outcome:
    success: bool
    output: dict[str, Any] = field(default_factory=dict)   # feeds forward into later steps' `have`
    text: str | None = None            # usually only set by the terminal step; becomes the AI reply text
    pending_delivery_id: int | None = None
    waiting_on: dict | None = None     # set if completion depends on an external event, e.g. {"event": "document_delivery", "delivery_id": ...}
```

---

## 2. Concrete worked example — reference capability

`find_customer` is the simplest real one in the registry. Use it as your template.

```python
def _resolve_find_customer(business, conversation, have):
    if have.get("customer_id"):
        customer = Customer.objects.filter(business=business, pk=have["customer_id"]).first()
        if customer is None:
            return Clarification("That customer couldn't be found — could you confirm who this is for?")
        return {"customer_id": customer.id, "_customer": customer}

    name = have.get("customer_name")
    if not name:
        return Clarification("Which customer is this for?")
    customer, candidates = find_matching_customer(business, name)
    if customer is None:
        if candidates:
            names = ", ".join(c.name for c in candidates)
            return Clarification(f"I found more than one customer close to \"{name}\" ({names}) — which one did you mean?")
        return Clarification(f"I couldn't find a customer named \"{name}\" on record.")
    return {"customer_id": customer.id, "_customer": customer}


def _execute_find_customer(business, resolved):
    return Outcome(success=True, output={"customer_id": resolved["customer_id"]})
```

Registry entry:
```python
"find_customer": Capability(
    name="find_customer", risk_tier="safe", required_inputs=set(), outputs={"customer_id"},
    side_effects=False, synchronous=True,
    resolve=_resolve_find_customer, execute=_execute_find_customer,
),
```

Note the `_customer` convention: keys prefixed with `_` (like `_customer`, `_entry`) carry live
Django model objects forward through `have` for convenience in later steps, while the
non-underscore key (`customer_id`) is the "official" declared output used for planner matching.

---

## 3. Step-by-step: adding a brand-new capability

### Step 1 — Write the real business logic outside `apps/agent/`
Put it in the owning app's `services.py` (e.g. `apps/sales/services.py`,
`apps/documents/services.py`). `apps/agent/capabilities.py` should only contain thin
resolve/execute wrappers — never duplicate business logic there. Existing capabilities import
from `apps.sales.services`, `apps.documents.services`, `apps.image_info_extractor.matching`,
etc. — follow the same pattern for your app.

### Step 2 — Write `_resolve_X` / `_execute_X` in `apps/agent/capabilities.py`
Add them near the bottom of the file, before the `CAPABILITIES` dict (line ~315), grouped under
the appropriate comment-banner section (`Pure lookups`, `Effectful, safe tier`, `Financial
tier`) or add a new banner section if your capability is a new kind.

Template:
```python
def _resolve_my_new_capability(business, conversation, have):
    # 1. Pull whatever you need out of `have` (already-resolved values from earlier steps)
    # 2. Re-validate against the real DB / external system
    # 3. Return a dict covering your declared `outputs`, OR a Clarification if you can't proceed
    ...


def _execute_my_new_capability(business, resolved):
    # Do the actual work (or pass through if resolve() already did it)
    result = my_app_services.do_the_thing(business, ...)
    return Outcome(success=True, output={"my_output_key": result.id})
```

### Step 3 — Register it in `CAPABILITIES` (`apps/agent/capabilities.py`, line ~315-364)
```python
CAPABILITIES = {
    ...  # existing entries, unchanged
    "my_new_capability": Capability(
        name="my_new_capability",
        risk_tier="safe",                     # see Step 6 before choosing "financial"/"dangerous"
        required_inputs={"customer_id"},      # keys that must be produced by some earlier capability
        outputs={"my_output_key"},            # keys this adds for later steps / the final reply
        side_effects=True,                    # True if it writes to DB or calls external API
        synchronous=True,                     # False only if completion waits on an external event
        resolve=_resolve_my_new_capability,
        execute=_execute_my_new_capability,
    ),
}
```

**This dict is the single safety chokepoint.** Nothing outside this file needs to change for the
capability to become theoretically reachable — but it still needs a *trigger path* (Step 4)
before anything actually calls it.

Naming rule for `required_inputs`/`outputs`: pick key names that either match an existing
capability's `outputs` (so the planner's backward-chaining in `compose_plan()` can auto-wire it
in), or that you will supply directly in the initial `have` dict from `plan_from_reply()`. If a
key you require isn't producible by anything, `compose_plan()` raises `PlanningError` at
composition time — the codebase's `_find_producer()` in `planner.py` line 40 does a linear scan
over `CAPABILITIES.items()` looking for a matching `outputs` entry.

### Step 4 — Decide how it gets triggered (pick exactly one)

**(a) Chains off an existing document type** (invoice / receipt / statement) — extend the
`_GENERATOR_FOR_DOC_TYPE` map in `apps/agent/planner.py` (line ~27):
```python
_GENERATOR_FOR_DOC_TYPE = {
    "invoice": "generate_document_from_entry",
    "receipt": "generate_document_from_entry",
    "statement": "generate_document_from_range",
    "my_doc_type": "my_new_capability",   # <-- add this
}
```
Then `plan_from_reply()` (planner.py line 72) will automatically backward-chain from your
capability through `compose_plan()` whenever the LLM's `draft_document.doc_type` matches
`"my_doc_type"`. No other planner code needs to change — this is the "genuinely dynamic" part
the module docstring describes.

**(b) A brand-new LLM-expressible intent** (not a document-generation chain at all) —
1. Extend `OUTPUT_CONTRACT_INSTRUCTIONS` in `apps/chat/prompt.py` so the LLM knows the new JSON
   shape it can emit for this intent.
2. Add/extend a serializer in `apps/chat/serializers.py` (alongside `AiReplySerializer` and the
   existing `DraftXSerializer` classes) to validate that new JSON shape.
3. Add code in `apps/chat/services.py` (`generate_reply`, or wherever the reply is dispatched)
   to route this new reply shape to your capability/plan, the same way `draft_document` is
   currently routed to `plan_from_reply()`.

**(c) Not LLM-triggered at all** — call it directly from a Django view/endpoint:
```python
from apps.agent.capabilities import CAPABILITIES

cap = CAPABILITIES["my_new_capability"]
resolved = cap.resolve(business, conversation, have={"customer_id": customer.id})
if isinstance(resolved, Clarification):
    ...  # surface resolved.message to the user
else:
    outcome = cap.execute(business, resolved)
```

### Step 5 — Test the plan/execute logic directly, without the LLM
```python
from apps.agent.planner import compose_plan
from apps.agent.executor import execute_plan

step_names = compose_plan("my_new_capability", have={"customer_id": 1, "doc_type": "my_doc_type"})
# assert step_names is the expected ordered list, e.g. ["find_customer", "my_new_capability"]
```
Do this before testing end-to-end through chat — it's deterministic and doesn't need Groq.

### Step 6 — Risk-tier gate (do not skip)

If your capability has `side_effects=True` and does something financial or otherwise
irreversible (recording a payment, deleting something, bulk operations, refunds, etc.):

- Set `risk_tier="financial"` or `risk_tier="dangerous"`.
- **Do NOT** wire it into automatic execution via `plan_from_reply` / `_GENERATOR_FOR_DOC_TYPE`.
  Auto-composed plans currently only ever reach `"safe"`-tier capabilities in practice (nothing
  in today's registry routes financial/dangerous capabilities through the planner).
- Instead, route it through the existing **tap-to-confirm pattern**: `ConfirmDraftBillView` /
  `ConfirmDraftActionView` in `apps/chat/views.py`. The user must explicitly tap/confirm in the
  UI before a financial/dangerous action executes. `record_payment` (the one financial-tier
  capability currently in the registry) is kept there specifically so its metadata is complete
  and reusable by a future composed goal, but it is *not* currently auto-wired into
  `plan_from_reply` — follow that same pattern for your new capability.
- This is a **conscious decision each time**, not a config flag — per the module docstring:
  "Adding one is a conscious safety decision, not a serializer tweak."

---

## 4. Full checklist (copy/paste per new capability)

- [ ] Business logic lives in the owning app's `services.py`, not duplicated in `apps/agent/`
- [ ] `_resolve_X(business, conversation, have)` written — returns dict or `Clarification`, never guesses
- [ ] `_execute_X(business, resolved)` written — returns an `Outcome`
- [ ] New `Capability(...)` entry added to `CAPABILITIES` dict in `apps/agent/capabilities.py`
      with correct `risk_tier`, `required_inputs`, `outputs`, `side_effects`, `synchronous`
- [ ] `required_inputs` keys are either producible by an existing capability's `outputs`, or
      supplied directly in the initial `have` dict at the trigger site
- [ ] Trigger path chosen and wired:
  - [ ] (a) `_GENERATOR_FOR_DOC_TYPE` entry added in `apps/agent/planner.py`, OR
  - [ ] (b) `OUTPUT_CONTRACT_INSTRUCTIONS` (prompt.py) + serializer (serializers.py) +
        dispatch code (services.py) added for a new LLM intent, OR
  - [ ] (c) called directly from a view with no LLM involvement
- [ ] Tested via `compose_plan()` / `execute_plan()` directly (no LLM) before chat testing
- [ ] If `risk_tier` is `"financial"` or `"dangerous"`: routed through
      `ConfirmDraftBillView`/`ConfirmDraftActionView` tap-confirm flow, explicitly NOT
      auto-wired into `plan_from_reply`

---

## 5. Where things live — quick reference table

| File | What to change |
|---|---|
| `apps/<your_app>/services.py` | New/extended business logic function your capability calls |
| `apps/agent/capabilities.py` | `_resolve_X`, `_execute_X` functions + new entry in `CAPABILITIES` dict (line ~315) |
| `apps/agent/planner.py` | `_GENERATOR_FOR_DOC_TYPE` dict (line ~27) — only if triggering off a `doc_type` |
| `apps/agent/results.py` | Only touch if you need a genuinely new result shape (rare — `Clarification`/`Outcome` almost always suffice) |
| `apps/agent/executor.py` | Only touch if you need new step-execution behavior (rare — read it, don't usually edit it) |
| `apps/chat/prompt.py` | `OUTPUT_CONTRACT_INSTRUCTIONS` — only for a brand-new LLM-expressible intent |
| `apps/chat/serializers.py` | `AiReplySerializer` / new `DraftXSerializer` — only for a brand-new LLM-expressible intent |
| `apps/chat/services.py` | `generate_reply` dispatch logic — only for a brand-new LLM-expressible intent |
| `apps/chat/views.py` | `ConfirmDraftBillView` / `ConfirmDraftActionView` — only for financial/dangerous capabilities |

No YAML/JSON config files exist for agents — everything is plain Python, defined directly in the
files above.

---

## 6. Further reading

- `AGENTS.md` (repo root) — narrative explanation of the whole system.
- `AGENT_DATA_FLOW.md` (repo root) — the original detailed call chain and data-shape reference
  this guide is built on top of.
