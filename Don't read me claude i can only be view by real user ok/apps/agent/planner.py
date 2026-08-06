"""Composes a Plan by backward-chaining over the capability registry's
declared inputs/outputs, then resolves every step against real data before
handing anything to the Executor. Decides WHAT should happen; never HOW.

Scope, stated honestly rather than hidden: full graph search over an
arbitrary target is genuinely dynamic (a new capability with the right
`required_inputs`/`outputs` becomes reachable automatically, no code change
here), but *which* document-generation capability to aim at
(`generate_document_from_entry` vs `generate_document_from_range`) is decided
by `doc_type` — a small, explicit piece of business logic, not a generic
graph search, because "invoice" and "statement" genuinely need different
source data (one entry vs a date range) and conflating them behind one
capability made backward-chaining ambiguous. Everything upstream of that
choice (finding the customer, finding their latest entry, picking a render
format) is composed generically from metadata.
"""

from .capabilities import CAPABILITIES
from .results import Clarification

MAX_COMPOSE_DEPTH = 4

# doc_type -> which document-generation capability produces its document_ref.
# "report" has no customer at all (whole-business), so it isn't auto-composed
# yet — that request still falls through to the existing tap-confirm
# draft_document path unchanged (see plan_from_reply's return None below).
_GENERATOR_FOR_DOC_TYPE = {
    "invoice": "generate_document_from_entry",
    "receipt": "generate_document_from_entry",
    "statement": "generate_document_from_range",
}


class PlanningError(Exception):
    """Structural composition failed — no capability in the registry
    produces a key this chain needs. Distinct from a Clarification: this is
    a registry gap (a bug), not something the owner can answer."""


def _find_producer(key):
    for name, cap in CAPABILITIES.items():
        if key in cap.outputs:
            return name
    return None


def compose_plan(target, have, *, _depth=0, _building=None):
    """Backward-chains from `target` to an ordered list of capability names,
    given the params already known in `have`. Depth-bounded recursive search
    over ~10 capabilities — not a general planner, but genuinely dynamic in
    the sense described in the module docstring.
    """
    if _building is None:
        _building = []
    if _depth > MAX_COMPOSE_DEPTH:
        raise PlanningError(f"composition too deep resolving {target!r}")

    cap = CAPABILITIES[target]
    missing = sorted(k for k in cap.required_inputs if k not in have)
    for key in missing:
        producer_name = _find_producer(key)
        if producer_name is None:
            raise PlanningError(f"no capability produces required input {key!r}")
        if producer_name not in _building:
            compose_plan(producer_name, have, _depth=_depth + 1, _building=_building)

    if target not in _building:
        _building.append(target)
    return _building


def plan_from_reply(business, conversation, message, reply_data):
    """Returns a resolved step list (`[{"capability": name, "resolved": dict}, ...]`)
    ready for the Executor, a `Clarification` if something couldn't be
    resolved, or `None` if this reply doesn't describe anything the agent
    layer auto-composes (plain text, draft_bill, draft_action, or a
    draft_document type not yet supported here — all of those keep working
    exactly as before, unchanged by this module).
    """
    draft = reply_data.get("draft_document")
    if not isinstance(draft, dict):
        return None

    doc_type = draft.get("doc_type")
    generator = _GENERATOR_FOR_DOC_TYPE.get(doc_type)
    if generator is None:
        return None

    have = {
        "doc_type": doc_type,
        "customer_id": draft.get("customer_id"),
        "date_from": draft.get("date_from"),
        "date_to": draft.get("date_to"),
        "requested_format": draft.get("format"),
    }

    try:
        step_names = compose_plan(generator, have) + ["send_whatsapp_document"]
    except PlanningError:
        # A registry gap, not an owner-facing question — fall through to the
        # existing tap-confirm draft_document path rather than surfacing an
        # internal error.
        return None

    resolved_have = dict(have)
    steps = []
    for name in step_names:
        cap = CAPABILITIES[name]
        result = cap.resolve(business, conversation, resolved_have)
        if isinstance(result, Clarification):
            return result
        resolved_have.update(result)
        steps.append({"capability": name, "resolved": result})
    return steps
