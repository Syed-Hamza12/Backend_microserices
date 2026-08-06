"""The capability registry — metadata-driven building blocks the Planner
(see planner.py) composes by matching declared outputs to declared required
inputs, instead of containing per-intent routing code.

Every `resolve`/`execute` here is a thin wrapper over an existing service
function in apps.sales / apps.documents / apps.image_info_extractor — no
business logic is duplicated. See each capability's comment for which
function it wraps.

No "delete_entry", "reverse_payment", or "bulk_*" capability is ever added to
this dict. That omission IS the dangerous-tier guarantee: a capability that
doesn't exist here cannot be reached by the Planner, the Executor, or any
future one composed on top of them, regardless of what a model outputs.
Adding one is a conscious safety decision, not a serializer tweak.
"""

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Callable, Literal, Optional

from apps.customers.models import Customer
from apps.documents import services as document_services
from apps.image_info_extractor.matching import find_matching_customer
from apps.sales import services as sales_services
from apps.sales.business_date import BusinessDateError, resolve as resolve_business_date
from apps.sales.models import ActivityEntry
from apps.sales.serializers import MAX_MONEY, MIN_MONEY

from .results import Clarification, Outcome

RiskTier = Literal["safe", "financial", "dangerous"]

# doc_type -> the ActivityEntry.type a "latest entry" lookup needs.
_ENTRY_TYPE_FOR_DOC = {"invoice": "sale", "receipt": "payment"}


@dataclass
class Capability:
    name: str
    risk_tier: RiskTier
    required_inputs: set
    outputs: set
    side_effects: bool
    synchronous: bool
    resolve: Callable
    execute: Callable
    verify: Optional[Callable] = None
    emits_events: frozenset = field(default_factory=frozenset)
    supported_doc_types: Optional[set] = None


# ---------------------------------------------------------------------------
# Pure lookups — no side effects, always safe, always synchronous.
# ---------------------------------------------------------------------------

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


def _resolve_find_latest_entry(business, conversation, have):
    customer = have.get("_customer") or Customer.objects.filter(business=business, pk=have.get("customer_id")).first()
    if customer is None:
        return Clarification("Which customer is this for?")
    entry_type = have.get("entry_type") or _ENTRY_TYPE_FOR_DOC.get(have.get("doc_type"))
    if entry_type is None:
        return Clarification("I'm not sure what kind of record to look for.")
    entry = document_services.resolve_latest_entry_for_customer(business, customer, entry_type)
    if entry is None:
        kind = "sale" if entry_type == "sale" else "payment"
        return Clarification(f"{customer.name} has no {kind} on record yet.")
    return {"entry_id": entry.id, "_entry": entry}


def _execute_find_latest_entry(business, resolved):
    return Outcome(success=True, output={"entry_id": resolved["entry_id"]})


def _resolve_choose_rendering_format(business, conversation, have):
    doc_type = have.get("doc_type")
    if doc_type not in document_services.DEFAULT_FORMAT:
        return Clarification(f"I don't know how to prepare a {doc_type}.")
    requested = have.get("requested_format")
    if requested and requested in document_services.SUPPORTED_FORMATS[doc_type]:
        return {"format": requested}
    return {"format": document_services.DEFAULT_FORMAT[doc_type]}


def _execute_choose_rendering_format(business, resolved):
    return Outcome(success=True, output={"format": resolved["format"]})


def _resolve_get_balance(business, conversation, have):
    customer = have.get("_customer") or Customer.objects.filter(business=business, pk=have.get("customer_id")).first()
    if customer is None:
        return Clarification("Which customer's balance do you want?")
    return {"_customer": customer}


def _execute_get_balance(business, resolved):
    customer = resolved["_customer"]
    return Outcome(success=True, output={"balance": str(customer.current_balance)})


# ---------------------------------------------------------------------------
# Effectful, safe tier — a document that already exists, sent as-is.
# ---------------------------------------------------------------------------

def _resolve_document_common(business, doc_type, customer, entry, have):
    try:
        doc_type, customer, date_from, date_to, entry = document_services.resolve_document_request(
            business,
            doc_type=doc_type,
            customer=customer,
            date_from=have.get("date_from"),
            date_to=have.get("date_to"),
            entry=entry,
        )
    except document_services.DocumentError as exc:
        return Clarification(exc.message, code=exc.code)
    # Wrapped under "document_ref" — this capability's declared output key —
    # so the Planner's resolve-phase walk (see planner.plan_from_reply) can
    # thread it forward into send_whatsapp_document's `have` the same way
    # every other step's output threads forward. execute() below just passes
    # this straight through; the real work already happened here, in
    # resolve() — resolution is what re-validates against the database.
    return {
        "document_ref": {
            "doc_type": doc_type,
            "_customer": customer,
            "_entry": entry,
            "date_from": date_from,
            "date_to": date_to,
            "format": have.get("format"),
        }
    }


def _resolve_generate_document_from_entry(business, conversation, have):
    entry = have.get("_entry")
    if entry is None and have.get("entry_id"):
        entry = ActivityEntry.objects.filter(business=business, pk=have["entry_id"]).first()
    customer = have.get("_customer")
    if customer is None and entry is not None:
        # find_latest_entry doesn't hand a customer object forward (only
        # entry_id) — the entry's own customer is authoritative here and
        # already implicitly verified (it's how the entry was matched).
        customer = entry.customer
    elif customer is None and have.get("customer_id"):
        customer = Customer.objects.filter(business=business, pk=have["customer_id"]).first()
    return _resolve_document_common(business, have.get("doc_type"), customer, entry, have)


def _resolve_generate_document_from_range(business, conversation, have):
    customer = have.get("_customer")
    if customer is None and have.get("customer_id"):
        customer = Customer.objects.filter(business=business, pk=have["customer_id"]).first()
    return _resolve_document_common(business, have.get("doc_type"), customer, None, have)


def _execute_generate_document(business, resolved):
    """No FastAPI render call happens here — rendering currently happens
    inside the same async job as the WhatsApp send
    (`apps.documents.delivery.handle_document_send_job`), for efficiency.
    `resolved` is already `{"document_ref": {...}}` (see the resolvers
    above) — this step exists as its own capability, distinct from
    send_whatsapp_document, so a future OCR-preview or print-only flow can
    stop here; today it's a pass-through.
    """
    return Outcome(success=True, output=resolved)


def _resolve_send_whatsapp_document(business, conversation, have):
    document_ref = have.get("document_ref")
    if not document_ref:
        return Clarification("I don't have a document ready to send yet.")
    if not business.gateway_session_id:
        return Clarification("WhatsApp isn't connected for this business yet — connect it in Settings first.")

    # `gateway_session_id` being set only means a session was created at
    # some point — it does NOT mean the phone is still linked right now.
    # Unlinking WhatsApp from the phone's own Linked Devices menu ends the
    # session on the gateway side without ever clearing this field in
    # Django, so a stale id here previously let the agent believe it could
    # send when nothing was actually connected. A live status check is the
    # only way to know for certain immediately before attempting to send —
    # it is deliberately NOT done anywhere more frequent than this (e.g.
    # not on every chat turn), so it costs one extra request only when a
    # send is actually about to happen.
    from apps.whatsapp import gateway_client
    from apps.whatsapp.gateway_client import GatewayError

    try:
        session = gateway_client.get_status(business.gateway_session_id)
    except GatewayError as exc:
        # GATEWAY_UNREACHABLE means OUR gateway service didn't answer — a
        # backend-side outage, nothing the owner can fix from Settings. It
        # was previously folded into the same "connect it in Settings"
        # message as a genuinely unlinked phone, which sent the owner to
        # tap a Settings button that does nothing for a problem on our end,
        # and — worse — meant "gateway is down" and "you never connected
        # WhatsApp" were indistinguishable from the reply text alone.
        if exc.code == "GATEWAY_UNREACHABLE":
            return Clarification("WhatsApp sending is temporarily unavailable — please try again shortly.")
        return Clarification("WhatsApp isn't connected for this business yet — connect it in Settings first.")
    if session.get("status") != "CONNECTED":
        return Clarification("WhatsApp isn't connected for this business yet — connect it in Settings first.")

    customer = document_ref.get("_customer")
    if not customer or not customer.phone:
        return Clarification("That customer has no phone number on record.")
    return document_ref


# The model's own reply text is written before the agent layer has decided
# whether this will auto-execute or fall back to a tap — it can't know which,
# so it can't safely describe a tap that may not exist. This capability owns
# the outcome text for the case that DOES auto-execute, in the owner's chosen
# reply language, following the same "never claim delivered" rule as the rest
# of the prompt (see apps.chat.prompt's SENDING section).
_SENDING_TEXT = {
    "en": "On it — preparing that now and sending it over WhatsApp.",
    "ur": "ٹھیک ہے، تیار کر کے واٹس ایپ پر بھیج رہا ہوں۔",
    "roman_ur": "Theek hai, taiyar kar ke WhatsApp par bhej raha hoon.",
}


def _execute_send_whatsapp_document(business, resolved):
    outcome_dict = document_services.queue_document_send(
        business,
        doc_type=resolved["doc_type"],
        customer=resolved.get("_customer"),
        entry=resolved.get("_entry"),
        date_from=resolved.get("date_from"),
        date_to=resolved.get("date_to"),
        requested_format=resolved.get("format"),
    )
    if not outcome_dict["sent"]:
        reason_text = {
            "NOT_CONNECTED": "WhatsApp isn't connected for this business yet — connect it in Settings first.",
            "NO_PHONE": "That customer has no phone number on record.",
            "NOT_ON_PLAN": "Sending documents on WhatsApp isn't included in the current plan.",
            "QUOTA_EXCEEDED": "The monthly WhatsApp sending limit has been reached.",
        }.get(outcome_dict["reason"], "That couldn't be sent right now.")
        return Outcome(success=False, text=reason_text)
    return Outcome(
        success=True,
        text=_SENDING_TEXT.get(business.language, _SENDING_TEXT["en"]),
        pending_delivery_id=outcome_dict["delivery_id"],
        output={"delivery_id": outcome_dict["delivery_id"], "job_id": outcome_dict["job_id"]},
        waiting_on={"event": "document_delivery", "delivery_id": outcome_dict["delivery_id"]},
    )


# ---------------------------------------------------------------------------
# Financial tier — always requires authorization, resolvability aside.
# These wrap apps.sales.services verbatim; kept here so the registry (and its
# metadata) is complete, and so a future composed goal like "record a payment
# then send the updated statement" can reason about them. The existing
# tap-confirm views (ConfirmDraftBillView/ConfirmDraftActionView) do not call
# through this registry yet — see the plan's §4 for why that refactor was
# deferred rather than risked without full regression coverage.
# ---------------------------------------------------------------------------

def _clean_amount(value, field_name):
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"The proposed {field_name} isn't a valid number.")
    if amount < MIN_MONEY:
        raise ValueError(f"The proposed {field_name} must be greater than zero.")
    if amount > MAX_MONEY:
        raise ValueError(f"The proposed {field_name} is unrealistically large.")
    return amount


def _resolve_record_payment(business, conversation, have):
    customer = have.get("_customer") or Customer.objects.filter(business=business, pk=have.get("customer_id")).first()
    if customer is None:
        return Clarification("Which customer is this payment from?")
    try:
        amount = _clean_amount(have.get("amount"), "amount")
    except ValueError as exc:
        return Clarification(str(exc))
    try:
        business_date = resolve_business_date(have.get("date"))
    except BusinessDateError as exc:
        return Clarification(str(exc))
    return {"_customer": customer, "amount": amount, "method": have.get("method"), "date": business_date}


def _execute_record_payment(business, resolved):
    from apps.sales.business_date import to_entry_timestamp

    entry = sales_services.record_payment(
        business=business,
        customer=resolved["_customer"],
        amount=resolved["amount"],
        method=resolved.get("method"),
        date=to_entry_timestamp(resolved["date"]) if resolved.get("date") else None,
        created_by="ai_chat",
    )
    return Outcome(success=True, output={"entry_id": entry.id})


CAPABILITIES = {
    "find_customer": Capability(
        name="find_customer", risk_tier="safe", required_inputs=set(), outputs={"customer_id"},
        side_effects=False, synchronous=True,
        resolve=_resolve_find_customer, execute=_execute_find_customer,
    ),
    "find_latest_entry": Capability(
        # "doc_type" not "entry_type": doc_type is already known from the
        # owner's request (it's what the whole plan is for), and the resolver
        # derives the ActivityEntry type from it — declaring "entry_type" here
        # would send backward-chaining looking for a capability that produces
        # it, and none does.
        name="find_latest_entry", risk_tier="safe", required_inputs={"customer_id", "doc_type"},
        outputs={"entry_id"}, side_effects=False, synchronous=True,
        resolve=_resolve_find_latest_entry, execute=_execute_find_latest_entry,
    ),
    "choose_rendering_format": Capability(
        name="choose_rendering_format", risk_tier="safe", required_inputs={"doc_type"}, outputs={"format"},
        side_effects=False, synchronous=True,
        resolve=_resolve_choose_rendering_format, execute=_execute_choose_rendering_format,
    ),
    "get_balance": Capability(
        name="get_balance", risk_tier="safe", required_inputs={"customer_id"}, outputs={"balance"},
        side_effects=False, synchronous=True,
        resolve=_resolve_get_balance, execute=_execute_get_balance,
    ),
    "generate_document_from_entry": Capability(
        name="generate_document_from_entry", risk_tier="safe", required_inputs={"doc_type", "entry_id", "format"},
        outputs={"document_ref"}, side_effects=True, synchronous=True,
        supported_doc_types={"invoice", "receipt"},
        resolve=_resolve_generate_document_from_entry, execute=_execute_generate_document,
    ),
    "generate_document_from_range": Capability(
        name="generate_document_from_range", risk_tier="safe", required_inputs={"doc_type", "customer_id", "format"},
        outputs={"document_ref"}, side_effects=True, synchronous=True,
        supported_doc_types={"statement"},
        resolve=_resolve_generate_document_from_range, execute=_execute_generate_document,
    ),
    "send_whatsapp_document": Capability(
        name="send_whatsapp_document", risk_tier="safe", required_inputs={"document_ref"},
        outputs={"delivery_id"}, side_effects=True, synchronous=False,
        emits_events=frozenset({"document_delivery"}),
        resolve=_resolve_send_whatsapp_document, execute=_execute_send_whatsapp_document,
    ),
    "record_payment": Capability(
        name="record_payment", risk_tier="financial", required_inputs={"customer_id", "amount"},
        outputs={"entry_id"}, side_effects=True, synchronous=True,
        resolve=_resolve_record_payment, execute=_execute_record_payment,
    ),
}
