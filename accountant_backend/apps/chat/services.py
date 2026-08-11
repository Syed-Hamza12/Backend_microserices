import json
import logging
import re

from django.utils import timezone

from apps.billing.models import Subscription
from apps.billing.services import refund_feature_usage
from apps.customers.models import Customer
from apps.image_info_extractor import matching
from apps.sales.business_date import BusinessDateError, business_today, resolve as resolve_business_date

from . import prompt
from .google_client import call_gemini_reasoning, call_gemini_text, call_gemma_planner
from .models import ChatMessage
from .serializers import AiReplySerializer

logger = logging.getLogger(__name__)

# Keyed by the same language codes as Business.language. Previously this was
# a single hardcoded English dict used regardless of the owner's chosen
# language — so a Roman Urdu (or Urdu) business hitting this path (model
# quota exhausted, both attempts failed to parse, etc.) saw an English
# sentence appear in an otherwise all-Roman-Urdu conversation. `speech_text`
# is written out by hand rather than derived via `to_urdu_script` for two
# reasons: this text is fixed and known-correct up front, so there is
# nothing to gain from a round trip through another model call on a path
# that already means "something just failed" — and `generate_reply`'s own
# roman_ur speech_text re-derivation is deliberately skipped when
# `ai_failed` (see its `and not ai_failed` guard), so leaving speech_text
# empty here meant Replay had nothing to speak and the app's own "this
# reply can't be spoken aloud" fallback kicked in — which is the second bug
# this fixes.
FALLBACK_REPLIES = {
    "en": {
        "text": "Sorry, I couldn't process that right now — please try again.",
        "speech_text": None,
    },
    "ur": {
        "text": "معذرت، ابھی اس کا جواب نہیں دے سکا — دوبارہ کوشش کریں۔",
        "speech_text": None,
    },
    "roman_ur": {
        "text": "Maazrat, abhi iska jawab nahi de saka — dobara koshish karein.",
        "speech_text": "معذرت، ابھی اس کا جواب نہیں دے سکا — دوبارہ کوشش کریں۔",
    },
}


# "Undo that" / "galat tha, wapas karo" is handled deterministically, never by
# the model: apps.sales.services.undo_pending_action already carries its own
# safety (5-minute window, single-use claim via a conditional UPDATE), and
# routing this through the LLM would mean trusting free text to decide
# whether to revert a money record — exactly the class of decision this
# codebase otherwise never lets the model make unsupervised. A regex catching
# the intent and a direct service call is strictly safer than a prompt
# instruction telling the model "call undo when they mean it."
UNDO_INTENT_PATTERN = re.compile(
    r"\bundo\b|\brevert\b|\bcancel that\b"
    r"|\bwapas\b|\bulta\b|\bpehle wala\b"
    r"|واپس|الٹا",
    re.IGNORECASE,
)

UNDO_REPLIES = {
    "en": {"done": "Done — that's reverted to what it was before.", "none": "Nothing to undo right now — the undo window may have expired."},
    "ur": {"done": "ٹھیک ہے، پہلے جیسا کر دیا۔", "none": "ابھی واپس کرنے کے لیے کچھ نہیں ہے — وقت ختم ہو چکا ہوگا۔"},
    "roman_ur": {"done": "Theek hai, pehle jaisa kar diya.", "none": "Abhi wapas karne ke liye kuch nahi hai — waqt khatam ho chuka hoga."},
}


def _try_handle_undo_intent(business, conversation, text, language):
    """Returns an `ai_message` if this turn was handled as an undo request,
    else None (meaning: proceed with the normal model turn). Kept separate
    from generate_reply's main body so the model path is untouched when no
    undo intent is present — this only short-circuits when the pattern hits."""
    if not UNDO_INTENT_PATTERN.search(text or ""):
        return None

    from apps.sales import services as sales_services
    from apps.sales.models import PendingUndo

    pending = (
        PendingUndo.objects.filter(business=business, used=False, expires_at__gt=timezone.now())
        .order_by("-created_at")
        .first()
    )
    localized = UNDO_REPLIES.get(language, UNDO_REPLIES["en"])
    if pending is None:
        reply_text = localized["none"]
    else:
        try:
            sales_services.undo_pending_action(pending_undo=pending)
            reply_text = localized["done"]
        except ValueError:
            reply_text = localized["none"]

    ChatMessage.objects.create(conversation=conversation, sender="owner", text=text)
    return ChatMessage.objects.create(conversation=conversation, sender="ai", text=reply_text)


def _fallback_reply(language):
    localized = FALLBACK_REPLIES.get(language, FALLBACK_REPLIES["en"])
    return {
        "text": localized["text"],
        "speech_text": localized["speech_text"],
        "draft_bill": None,
        "document_ready": None,
        "draft_action": None,
        "draft_document": None,
    }

STRICTER_REMINDER = (
    "\n\nIMPORTANT: your last response did not match the required JSON shape exactly. "
    "Reply again with ONLY the JSON object, no markdown fences, no extra text."
)


#: Above this, the text is not a spoken sentence and something is wrong —
#: transliteration is for one dictated message, not a document.
MAX_TRANSLITERATE_CHARS = 1000

#: Urdu script lives in these blocks. Latin-only text needs no round trip.
URDU_SCRIPT_RANGE = ("؀", "ۿ")


def has_urdu_script(text):
    return any(URDU_SCRIPT_RANGE[0] <= ch <= URDU_SCRIPT_RANGE[1] for ch in text)


def transliterate_to_roman_urdu(text):
    """Urdu script -> Roman Urdu for a dictated message.

    Returns the input unchanged if it holds no Urdu script or the call fails:
    showing the owner their words in the wrong script is a much smaller problem
    than losing what they just said, so this never raises.
    """
    text = (text or "").strip()
    if not text or not has_urdu_script(text):
        return text

    try:
        # The reasoning/quality tier's own key pool (call_gemini_text), not
        # OCR's — see call_gemini_reasoning's docstring for why chat traffic
        # needed its own pool separate from OCR's.
        converted = call_gemini_text(
            prompt.TRANSLITERATION_INSTRUCTIONS, text[:MAX_TRANSLITERATE_CHARS]
        )
    except Exception as exc:  # noqa: BLE001 - never lose the owner's words to this
        logger.warning("transliteration failed, returning original text: %s", exc)
        return text

    converted = (converted or "").strip()
    # A model that answered instead of transliterating would put Urdu script
    # back in the reply — that is a failed conversion, not a result.
    if not converted or has_urdu_script(converted):
        logger.warning("transliteration returned unusable output; keeping original.")
        return text
    return converted


#: Accepted `language` values on the chat endpoint — the same codes
#: `Business.language` uses, so the two can never disagree.
VALID_LANGUAGES = {"en", "ur", "roman_ur"}


def resolve_language(requested, business):
    """The language this reply must be written in.

    Prefers what the app sent with the request, because that is the owner's
    live Settings choice. Falls back to `business.language` for older clients
    that send nothing, and ignores anything unrecognised rather than letting a
    bad value silently reach the prompt.
    """
    if requested in VALID_LANGUAGES:
        return requested
    if requested:
        logger.warning("Ignoring unrecognised chat language %r.", requested)
    return business.language


def _history_limit(business):
    subscription = Subscription.active_for(business)
    return subscription.resolved_chat_history_limit() if subscription else 15


def _recent_history(conversation, limit):
    # Error fallbacks are excluded: they are the app apologising, not the
    # assistant reasoning, and replaying them as prior turns both wastes the
    # history budget and nudges the model toward repeating them.
    messages = list(
        conversation.messages.filter(is_error_fallback=False).order_by("-timestamp", "-id")[:limit]
    )
    messages.reverse()
    return messages


def _parse_and_validate(raw_content):
    data = json.loads(raw_content)
    serializer = AiReplySerializer(data=data)
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data


def link_drafted_customer(business, draft_bill):
    """Resolve a draft's `customer_name_guess` to a real customer, in place.

    The model is handed the customer list and asked to fill `customer_id`, and
    it still returns null for near-misses — it would not match the guess "papa"
    against the only customer on the account, named "pap". An unlinked draft
    cannot be confirmed at all (the confirm view rejects it with
    CUSTOMER_NOT_MATCHED), so the owner is left re-tapping a button that can
    never succeed.

    This reuses the photo path's matcher rather than adding a second, laxer
    notion of "close enough": it links only on a clear, unambiguous single
    match, and leaves the draft unlinked whenever two customers are plausible.
    Money landing on the wrong ledger is the worse failure, so an ambiguous
    guess still becomes a question for the owner.
    """
    if not isinstance(draft_bill, dict):
        return

    if not draft_bill.get("customer_id"):
        customer, _candidates = matching.find_matching_customer(
            business, draft_bill.get("customer_name_guess")
        )
        if not customer:
            return
        draft_bill["customer_id"] = str(customer.id)
        draft_bill["customer_name_guess"] = None
        # Take the balance from the ledger, never the model's guess at it — it
        # reported 0 for a customer who was owed 525.
        draft_bill["previous_balance"] = float(customer.current_balance)
        logger.info("linked chat draft to customer %s by name match", customer.id)
    else:
        customer = Customer.objects.filter(
            business=business, pk=draft_bill["customer_id"]
        ).first()
        if not customer:
            return

    # The card and the Edit Draft screen show the customer's name, and the only
    # name ever sent was `customer_name_guess` — which is cleared the moment a
    # draft is linked to a real customer. So a *correctly* matched draft showed
    # a blank customer, and matching more drafts made it more common. Send the
    # real name alongside the id.
    draft_bill["customer_name"] = customer.name


def link_drafted_payment_customer(business, draft_payment):
    """Same as `link_drafted_customer` but for a `draft_payment` — resolves
    `customer_name_guess` to a real `customer_id` in place, so the card
    shows a name immediately rather than waiting for confirm-time linking
    to discover the same match."""
    if not isinstance(draft_payment, dict):
        return

    if not draft_payment.get("customer_id"):
        customer, _candidates = matching.find_matching_customer(
            business, draft_payment.get("customer_name_guess")
        )
        if not customer:
            return
        draft_payment["customer_id"] = str(customer.id)
        draft_payment["customer_name_guess"] = None
    else:
        customer = Customer.objects.filter(business=business, pk=draft_payment["customer_id"]).first()
        if not customer:
            return

    draft_payment["customer_name"] = customer.name


#: How recent a matching sale has to be to count as a likely accidental
#: repeat of THIS save_now request, not a genuine second order the owner
#: placed shortly after the first. Short on purpose — a real repeat order
#: ("same 2 packets again tomorrow") is common and must never be blocked;
#: this only catches the case of the same instruction landing twice within
#: the same conversational turn (a double-tap, a retried message, the model
#: re-emitting save_now on a message that already went through).
DUPLICATE_SALE_WINDOW_MINUTES = 15


def _find_recent_matching_sale(business, customer, items, payment_received):
    """The most recent sale for this customer, in the last
    `DUPLICATE_SALE_WINDOW_MINUTES`, with the same item names/quantities and
    the same amount received — i.e. one that looks like the exact same bill
    save_now is about to write again. None if nothing matches."""
    from apps.sales.models import ActivityEntry

    cutoff = timezone.now() - timezone.timedelta(minutes=DUPLICATE_SALE_WINDOW_MINUTES)
    candidate_key = sorted(
        (li["item_name"], str(li["quantity"]), str(li["rate"])) for li in items
    )
    recent_sales = (
        ActivityEntry.objects.filter(
            business=business, customer=customer, type="sale", created_at__gte=cutoff,
        )
        .prefetch_related("line_items")
        .order_by("-created_at")[:5]
    )
    for entry in recent_sales:
        entry_key = sorted(
            (li.item_name, str(li.quantity), str(li.rate)) for li in entry.line_items.all()
        )
        if entry_key == candidate_key:
            return entry
    return None


#: Narrower than prompt.DOCUMENT_HINT_PATTERN (which also matches plain
#: accounting words like "ledger"/"hisaab"/"poora") — this one gates an
#: actual WhatsApp send attempt, so it must only fire on words that
#: genuinely mean "send this", never on a message that merely mentions
#: accounts/statements in passing.
_SEND_INTENT_PATTERN = re.compile(
    r"\bsend\b|\bshare\b|\bwhatsapp\b|\bbhej\b|\bbhejo\b|\bbhejna\b|\bbhej do\b"
    r"|بھیج|بھیجو|واٹس ایپ|واٹساپ",
    re.IGNORECASE,
)


def record_drafted_bill(business, message, *, also_send=False):
    """Records `message.draft_bill` on the ledger immediately, skipping Confirm.

    Only reached when the owner asked for it in words ("record mein save kar
    do") and the model set `draft_bill.save_now`. Confirm still exists and is
    still the default — this is the explicit-instruction shortcut, not a change
    to how ordinary drafts behave.

    Every guard the Confirm button relies on is deliberately kept, because none
    of them were about the tap: the same `build_sale_from_draft` validation, the
    same requirement that a real customer is linked, and the same audit row
    recording that the AI created this entry.

    Returns `(status, delivery)`. `status` is "saved" only if money was
    actually written, "duplicate" if an identical sale for this customer was
    already recorded moments ago (see `_find_recent_matching_sale` — this is
    the save_now path's equivalent of the owner's own "is this a repeat order
    or the same one already saved?" question, done automatically since
    save_now never shows a review step), or "failed" for a validation/DB
    error. `delivery` is always None unless `also_send` is true AND the sale
    was actually saved.

    `also_send`: the owner saying "save karo" is not the same as "save karo
    aur bhej do" — this only attempts a WhatsApp send (via the same
    `queue_invoice_send` the Confirm button uses, so the same NOT_CONNECTED/
    NO_PHONE/NOT_ON_PLAN/QUOTA_EXCEEDED outcomes apply) when the owner's own
    words asked for sending too (see `_SEND_INTENT_PATTERN`, checked by the
    caller). Silently never attempting a send the owner explicitly asked for
    — and then saying nothing about it — is exactly the confusing "did it
    send or not?" gap this parameter closes.
    """
    from apps.documents import services as document_services
    from apps.sales import services as sales_services
    from apps.sales.business_date import to_entry_timestamp

    from .views import build_sale_from_draft

    draft = message.draft_bill or {}
    customer = Customer.objects.filter(business=business, pk=draft.get("customer_id") or 0).first()
    if not customer:
        # Nothing to attach the money to. The draft stays unconfirmed so the
        # owner can pick a customer and confirm by hand.
        logger.info("save_now skipped: draft on message %s has no matched customer", message.id)
        return "failed", None

    try:
        items, payment_received, business_date = build_sale_from_draft(draft)
    except ValueError as exc:
        logger.warning("save_now rejected on message %s: %s", message.id, exc)
        return "failed", None

    if _find_recent_matching_sale(business, customer, items, payment_received) is not None:
        logger.info(
            "save_now skipped: an identical sale for customer %s was recorded in the last %s minutes",
            customer.id, DUPLICATE_SALE_WINDOW_MINUTES,
        )
        return "duplicate", None

    try:
        sale_entry, _payment = sales_services.record_sale(
            business=business,
            customer=customer,
            items=items,
            amount_received=payment_received,
            payment_method="cash" if payment_received else None,
            date=to_entry_timestamp(business_date) if business_date else None,
            created_by="ai_chat",
        )
    except Exception:  # noqa: BLE001 - a failed save must leave the draft confirmable by hand
        logger.exception("save_now failed to record the sale for message %s", message.id)
        return "failed", None

    sales_services.log_ai_created_sale(entry=sale_entry, source_message_id=message.id)
    ChatMessage.objects.filter(pk=message.pk).update(draft_confirmed=True)
    message.draft_confirmed = True
    logger.info("save_now recorded sale %s from message %s", sale_entry.id, message.id)

    delivery = document_services.queue_invoice_send(business, sale_entry, customer) if also_send else None
    return "saved", delivery


def to_urdu_script(text):
    """Roman Urdu -> native Urdu script, for text-to-speech.

    The mirror of [transliterate_to_roman_urdu]. Returns "" if the text is
    unusable or the call fails — the caller must treat an empty result as
    "don't speak this", never as "speak the Latin text instead".
    """
    text = (text or "").strip()
    if not text or has_urdu_script(text):
        return text

    try:
        # Back on Gemini (call_gemini_text) — this is in fact the one call in
        # this module Gemini was originally added FOR: Llama has a documented
        # history of writing Urdu script badly enough to be unintelligible
        # when spoken ("Mujhe samajh nahi aya" came back as "موجه سمجه نهين
        # آدا" — Arabic ه for Urdu ھ/ہ, mangled words). It was moved to Groq
        # for a time purely over Gemini's free-tier quota; that reasoning no
        # longer applies now that chat has its own dedicated key pool (see
        # call_gemini_reasoning's docstring). `has_urdu_script` below cannot
        # catch the mangled-script failure mode on its own: the mangled
        # output is still technically in the Urdu/Arabic Unicode block, so
        # it looks "valid" to that check. If TTS starts sounding like noise
        # again for Roman Urdu owners, this is the first place to look.
        converted = call_gemini_text(
            prompt.URDU_SCRIPT_INSTRUCTIONS, text[:MAX_TRANSLITERATE_CHARS]
        )
    except Exception as exc:  # noqa: BLE001 - speech is optional, the reply is not
        logger.warning("urdu-script conversion failed: %s", exc)
        return ""

    converted = (converted or "").strip()
    # No Urdu script in the "conversion" means it echoed the Latin text or
    # answered instead of converting. Speaking that through the ur-PK voice is
    # exactly the garbled noise this function exists to prevent.
    if not converted or not has_urdu_script(converted):
        logger.warning("urdu-script conversion returned unusable output.")
        return ""
    return converted


def select_model_tier(message_text, language):
    """Which model handles the JSON/intent step — two tiers, both Gemini now
    (see `_call_model`). Purely intent-complexity-driven: `needs_reasoning`
    recognises bill/edit/document intent in English, Urdu and Roman Urdu
    alike, and that alone decides fast (Gemma) vs reasoning
    (Gemini's quality model) for THIS step.

    Language used to force the reasoning tier by itself for ur/roman_ur,
    unconditionally — because the fast-tier model cannot WRITE Urdu
    acceptably (it once returned "پاپ کا جو 1 ڑڑ خَد 20 مۉّد" to a real
    owner, which is not words). That write-quality problem is real, but it
    is a problem with PROSE, not with understanding the message or filling
    out the JSON contract — so it is now solved downstream instead: for
    `ur`/`roman_ur`, the JSON step's own "text" is discarded entirely
    (never shown to the owner, see `generate_reply`), and
    `_write_final_reply` COMPOSES a fresh reply on the reasoning-tier model
    from the owner's message plus a plain execution summary — never by
    forcing the whole intent/planning step onto the expensive model, and
    never by rewriting the JSON step's own sentence. See AGENTS.md /
    AGENT_DATA_FLOW.md's model-routing sections for the full before/after
    picture and the token-cost reasoning behind this.

    Chat's two tiers each have their own Gemini key pool
    (`settings.FAST_GEMINI_API_KEYS` / `QUALITY_GEMINI_API_KEYS`), separate
    from OCR's (`GEMINI_API_KEYS`,
    `apps.image_info_extractor.gemini_client.extract_receipt_data`) — three
    features that would otherwise compete for one shared free-tier daily
    quota.
    """
    return "reasoning" if prompt.needs_reasoning(message_text) else "fast"


def _call_model(tier, messages):
    """The single dispatch point between the two chat-turn tiers. Both are
    Gemini now: "fast" is Gemma (`call_gemma_planner`), "reasoning" is
    Gemini's quality model (`call_gemini_reasoning`, replacing Groq's
    llama-3.3-70b-versatile) — `select_model_tier`'s routing rule itself is
    unchanged, only what each tier resolves to. Each keeps its own key pool
    (see settings.FAST_GEMINI_API_KEYS / QUALITY_GEMINI_API_KEYS)."""
    if tier == "fast":
        return call_gemma_planner(messages=messages)
    return call_gemini_reasoning(messages=messages)


#: Languages whose final reply text/speech_text are COMPOSED FRESH by the
#: reasoning-tier "response writer" step, from execution facts — never shown the JSON
#: step's own prose. English isn't here: the fast tier's English prose is fine, so
#: needs_reasoning alone already routes English's genuinely complex turns to
#: the reasoning tier for the whole call — no separate writer pass is needed for it.
_WRITER_LANGUAGES = ("ur", "roman_ur")


def _build_execution_summary(reply_data):
    """Plain, structured facts about what this turn produced — the ONLY
    description of "what happened" the response-writer step is given,
    besides the owner's own message. Deliberately never the JSON step's own
    "text" unless nothing else describes the turn at all (a plain question/
    answer with no draft) — see `generate_reply` for why: that field is
    the JSON step's own prose, and for `ur`/`roman_ur` turns it must never reach
    the owner directly, whether raw or "polished". Building this summary
    from the contract's own structured fields (draft_bill / draft_action's
    `summary` / draft_document's `summary`) keeps it purely factual.
    """
    draft_bill = reply_data.get("draft_bill")
    if isinstance(draft_bill, dict):
        bits = ["A sales bill was prepared"]
        if draft_bill.get("customer_name") or draft_bill.get("customer_name_guess"):
            bits.append(f"for customer {draft_bill.get('customer_name') or draft_bill.get('customer_name_guess')}")
        if draft_bill.get("total_amount") is not None:
            bits.append(f"total amount {draft_bill['total_amount']}")
        if draft_bill.get("payment_received"):
            bits.append(f"payment received {draft_bill['payment_received']}")
        status = (
            "it has already been recorded on the ledger"
            if draft_bill.get("save_now")
            else "it is awaiting the owner's confirmation before it is recorded"
        )
        return ", ".join(bits) + f"; {status}."

    draft_action = reply_data.get("draft_action")
    if isinstance(draft_action, dict) and draft_action.get("summary"):
        return f"{draft_action['summary']}. This change is awaiting the owner's confirmation."

    draft_document = reply_data.get("draft_document")
    if isinstance(draft_document, dict) and draft_document.get("summary"):
        return f"{draft_document['summary']}."

    # No draft at all — a plain question, answer, or clarification turn.
    # There is no structured fact to summarize, so the JSON step's own
    # "text" is used here purely as CONTENT for the writer to re-express in
    # its own words — never shown to the owner directly.
    return reply_data.get("text") or ""


def _write_final_reply(user_message, execution_summary, language):
    """The reasoning-tier 'response writer' step. Composes a BRAND NEW reply from the
    owner's original message plus a plain-language execution summary of
    what actually happened — it is never handed the JSON step's own
    sentence to rewrite. Deliberately does NOT receive the system prompt,
    business context, chat history, or the JSON contract: it cannot see or
    influence intent, planning, capabilities, or execution, only compose
    the final wording from facts it's told. See AGENTS.md's model-routing
    section for the full picture.

    Returns (text, speech_text). Falls back to the plain execution summary
    itself (still passed through `to_urdu_script` for roman_ur, so speech
    isn't silently lost) on any failure — a rough, factual fallback beats
    losing the reply.
    """
    execution_summary = (execution_summary or "").strip()
    user_content = f"OWNER'S MESSAGE:\n{user_message}\n\nEXECUTION SUMMARY:\n{execution_summary or '(none)'}"

    def _fallback():
        # No model output at all — the execution summary is the closest
        # thing to a factual reply available, so use it verbatim rather
        # than show nothing.
        fallback_text = execution_summary or user_message
        if language == "roman_ur":
            return fallback_text, (to_urdu_script(fallback_text) or None)
        return fallback_text, None

    try:
        if language == "roman_ur":
            raw = call_gemini_reasoning(
                messages=[
                    {"role": "system", "content": prompt.RESPONSE_WRITER_ROMAN_UR},
                    {"role": "user", "content": user_content},
                ],
            )
            composed = json.loads(raw)
            text = (composed.get("text") or "").strip()
            speech = (composed.get("speech_text") or "").strip()
            if not text or has_urdu_script(text):
                logger.warning("response writer (roman_ur) returned unusable text; using execution summary")
                return _fallback()
            return text, (speech or to_urdu_script(text) or None)

        # language == "ur"
        raw = call_gemini_reasoning(
            messages=[
                {"role": "system", "content": prompt.RESPONSE_WRITER_UR},
                {"role": "user", "content": user_content},
            ],
            response_format_json=False,
        )
        composed = (raw or "").strip()
        if not composed or not has_urdu_script(composed):
            logger.warning("response writer (ur) returned unusable text; using execution summary")
            return _fallback()
        return composed, None
    except Exception as exc:  # noqa: BLE001 - a rough fallback beats losing the reply
        logger.warning("response writer step failed, using execution summary: %s", exc)
        return _fallback()


def generate_reply(*, business, conversation, text, language=None):
    language = language or business.language

    undo_message = _try_handle_undo_intent(business, conversation, text, language)
    if undo_message is not None:
        return undo_message

    history = _recent_history(conversation, _history_limit(business))
    messages = prompt.build_messages(business, history, text, language=language, conversation=conversation)
    tier = select_model_tier(text, language)

    reply_data = None
    for attempt in range(2):
        try:
            raw = _call_model(tier, messages)
            candidate = _parse_and_validate(raw)
        except Exception as exc:  # noqa: BLE001 - covers Groq errors + bad-shape JSON
            logger.warning("chat reply attempt %s (%s) failed: %s", attempt, tier, exc)
            if attempt == 0:
                messages = messages[:-1] + [
                    {"role": "user", "content": text + STRICTER_REMINDER}
                ]
            continue

        # No script-leak retry here anymore: the JSON step's own "text" for
        # roman_ur/ur is a discarded placeholder (see below) — the response
        # writer composes the real reply from execution facts and is
        # responsible for correct script on its own output. Retrying the
        # JSON step over a field nobody sees would just burn an extra call.
        reply_data = candidate
        break

    ai_failed = reply_data is None
    if ai_failed:
        reply_data = _fallback_reply(language)
        # The owner's monthly AI quota was claimed before the call (it has to
        # be, to stay race-free). Every attempt failing means they got nothing,
        # so the slot goes back rather than silently costing them a message.
        refund_feature_usage(business, "ai_chat")
        logger.error("chat reply failed after all attempts for business %s", business.id)

    # The contract asks for speech_text on every Roman Urdu reply, and the model
    # routinely returns it empty anyway. The app then falls back to speaking
    # `text` — Latin letters pushed through the ur-PK voice, which comes out as
    # noise rather than words. Backfilling here keeps that fallback off the
    # Roman Urdu path entirely; an empty result means "stay silent", which is a
    # far better outcome than gibberish.
    link_drafted_customer(business, reply_data.get("draft_bill"))
    link_drafted_payment_customer(business, reply_data.get("draft_payment"))

    # For ur/roman_ur, the JSON step's own "text" is NEVER the final reply —
    # it is stored here only as a placeholder until the response-writer step
    # below composes the real one from execution facts. For every other
    # language (or a failed call, whose reply_data is already the fixed,
    # correctly-localized FALLBACK_REPLIES text) it IS the final reply.
    ChatMessage.objects.create(conversation=conversation, sender="owner", text=text)
    # A "report" has no single recipient — it can span every customer in the
    # business — so it can never be sent via the confirm/send pipeline
    # (/documents/send/ requires a resolvable phone number and 400s with
    # RECIPIENT_REQUIRED for report drafts with no customer_id). Converted
    # here, at creation time, into the same report_view shape
    # _attach_report_view produces, so it renders as a View button instead
    # of a Confirm & Send card regardless of what the owner's own wording
    # matched against.
    draft_document = reply_data.get("draft_document")
    report_view_from_draft = None
    if isinstance(draft_document, dict) and draft_document.get("doc_type") == "report":
        report_view_from_draft = _report_view_from_draft_document(draft_document)
        draft_document = None

    # An invoice/receipt draft only works if the customer actually has a
    # matching entry — ConfirmDraftDocumentView 400s otherwise, and since
    # that failure is deterministic (not a race), every retap of "Confirm
    # karein & Bhejein" would 400 identically. The model sometimes proposes
    # one anyway (even while its own `text` says "no payment on record"), so
    # this re-checks the same lookup the confirm view does and drops the
    # draft rather than showing a Confirm button that can never succeed.
    if isinstance(draft_document, dict) and draft_document.get("doc_type") in ("invoice", "receipt"):
        from apps.documents.services import resolve_latest_entry_for_customer

        customer_id = draft_document.get("customer_id")
        entry_type = "sale" if draft_document["doc_type"] == "invoice" else "payment"
        customer_obj = Customer.objects.filter(business=business, pk=customer_id).first() if customer_id else None
        if customer_obj is None or resolve_latest_entry_for_customer(business, customer_obj, entry_type) is None:
            draft_document = None

    ai_message = ChatMessage.objects.create(
        conversation=conversation,
        sender="ai",
        text=reply_data.get("text") or "",
        speech_text=(reply_data.get("speech_text") or "").strip() or None,
        draft_bill=reply_data.get("draft_bill"),
        document_ready=reply_data.get("document_ready"),
        draft_action=reply_data.get("draft_action"),
        draft_document=draft_document,
        report_view=report_view_from_draft,
        # These two were missing entirely until now: draft_customer/draft_payment
        # were validated by AiReplySerializer and even linked to a real customer
        # above, but never actually written to the ChatMessage row — so the
        # model could propose one and it would silently vanish, never reaching
        # the mobile app at all. Caught while wiring draft_payment in.
        draft_customer=reply_data.get("draft_customer"),
        draft_payment=reply_data.get("draft_payment"),
        # Marked so this apology isn't replayed into later prompts as if it were
        # a real assistant turn — feeding "sorry, I couldn't process that" back
        # as context teaches the model to produce more of the same.
        is_error_fallback=ai_failed,
    )

    # Recorded only when the owner asked for it in words. Done after the message
    # exists so the audit row can point at the message that caused the entry.
    save_now_failed = False
    save_now_duplicate = False
    delivery = None
    draft = reply_data.get("draft_bill") or {}
    if draft.get("save_now") and not ai_failed:
        # "save karo" alone must never send anything — only attempt a
        # WhatsApp send when the owner's own words also asked for it (see
        # _SEND_INTENT_PATTERN's docstring on record_drafted_bill).
        also_send = bool(_SEND_INTENT_PATTERN.search(text or ""))
        save_result, delivery = record_drafted_bill(business, ai_message, also_send=also_send)
        if save_result != "saved":
            save_now_failed = save_result == "failed"
            save_now_duplicate = save_result == "duplicate"
            if language not in _WRITER_LANGUAGES:
                # English: no response-writer step runs below, so this must
                # be said directly here, same as before. Say so rather than
                # leaving the owner believing it is on the ledger — the reply
                # text already claims it was saved, because the model was
                # told to only set save_now when it means it.
                note = (
                    "(An identical bill for this customer was already recorded moments ago — "
                    "not saved again. If this is a genuinely new order, please confirm it by hand.)"
                    if save_now_duplicate
                    else "(I could not record it automatically — please check the draft and confirm it.)"
                )
                ai_message.text = f"{ai_message.text}\n\n{note}"
                ai_message.speech_text = None
                ai_message.save(update_fields=["text", "speech_text"])
            # For ur/roman_ur this failure is folded into the execution
            # summary instead (see below) so the response writer composes it
            # in the owner's language, rather than appending an English
            # sentence to a reply that hasn't been written yet.
        elif delivery is not None and language not in _WRITER_LANGUAGES:
            # Saved successfully AND the owner asked for it to be sent —
            # English path: say plainly whether it actually went out, same
            # "never let the owner believe something happened that didn't"
            # reasoning as the failure/duplicate notes above.
            ai_message.text = f"{ai_message.text}\n\n{_delivery_note(delivery, 'en')}"
            ai_message.save(update_fields=["text"])

    agent_overwrote_text = False
    if not ai_failed:
        agent_overwrote_text = apply_safe_document_send(business, conversation, ai_message, reply_data, language)

    if language in _WRITER_LANGUAGES and not ai_failed and not agent_overwrote_text:
        # The response-writer step: NEVER given the JSON step's own "text"
        # to rewrite. It composes a brand new reply from the owner's message
        # plus a plain-language summary of what was actually decided/done —
        # see _build_execution_summary / _write_final_reply. This is the
        # ONLY place a Roman Urdu/Urdu owner's final reply text comes from;
        # the JSON step's own model output never reaches the owner directly.
        summary = _build_execution_summary(reply_data)
        if save_now_duplicate:
            summary = (summary + " " if summary else "") + (
                "An identical bill for this customer was already recorded moments ago, so this one "
                "was NOT saved again — tell the owner and ask whether it's a genuinely new/repeat "
                "order (in which case they should confirm it by hand) or the same one they already saved."
            )
        elif save_now_failed:
            summary = (summary + " " if summary else "") + (
                "The bill could NOT be recorded automatically due to an error — "
                "ask the owner to check the draft and confirm/save it manually."
            )
        elif delivery is not None:
            summary = (summary + " " if summary else "") + _delivery_summary_note(delivery)
        final_text, final_speech = _write_final_reply(text, summary, language)
        ai_message.text = final_text
        ai_message.speech_text = final_speech or None
        ai_message.save(update_fields=["text", "speech_text"])

    if not ai_failed and not agent_overwrote_text:
        _enforce_whatsapp_not_connected_notice(business, ai_message, text, language)

    if not ai_failed:
        # Deliberately NOT gated on gateway_session_id, unlike the notice
        # above: this business genuinely has WhatsApp linked, so that check
        # never fires here — but "linked at some point" is not the same as
        # "this specific bill actually went out", and the model has no real
        # signal either way. Caught a real case: asked "WhatsApp m send
        # hogya?" after a save_now bill, the model answered "Haan ji ...
        # send ho raha hai" — a fabricated in-progress claim that dodges
        # the prompt's existing "never say sent/delivered" wording by using
        # present-continuous phrasing instead of past tense. Answers with
        # the real DocumentDelivery status instead of deflecting — an
        # owner asking this needs a real answer ("not sent, reconnect
        # WhatsApp"), not "I can't see status, check the app".
        _answer_delivery_status_question(business, ai_message, text, language)

    if not ai_failed:
        _attach_report_view(business, ai_message, text)

    return ai_message


def _report_view_from_draft_document(draft_document):
    """Builds the same report_view shape `_attach_report_view` produces, but
    from the model's own structured `draft_document` (doc_type "report")
    rather than re-deriving it from the owner's raw text. Used at message
    creation so a report the model proposed always gets a View button
    rather than depending on the independent text-regex heuristic also
    matching the same message.
    """
    try:
        # None means "use today", same as resolve()'s own contract — matches
        # how ConfirmDraftDocumentView already treats a missing date_from/to.
        date_from = resolve_business_date(draft_document.get("date_from")) or business_today()
        date_to = resolve_business_date(draft_document.get("date_to")) or business_today()
    except BusinessDateError:
        return None

    summary = (
        f"Details for {date_from.isoformat()}"
        if date_from == date_to
        else f"Details from {date_from.isoformat()} to {date_to.isoformat()}"
    )
    return {
        "kind": "range",
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "summary": summary,
    }


def _attach_report_view(business, ai_message, owner_text):
    """Deterministically attaches `report_view` whenever the owner's message
    named a date/range together with query intent ("pichle hafte ki detail
    batao", "10 se 20 tareek ka hisaab"), OR asked a ranking question like
    "top 5 customers" — the mobile app then shows a "View" button that
    fetches the real data directly (GET /sales/entries/ for a date range,
    the Reports screen's customers tab for a ranking), rather than trusting
    anything the model summarized in "text". Deliberately not gated on
    anything the model itself produced: prompt-compliance is exactly what
    this codebase has repeatedly found unreliable for facts (dates, delivery
    status, DB contents — see this module's other deterministic checks), and
    these queries are the highest-stakes case yet, since the answer spans
    many customers at once.
    """
    text = owner_text or ""

    # Ranking has no date range at all — checked first and independently of
    # QUERY_HINT_PATTERN/date extraction below, which would otherwise never
    # match "top 5 customers" (no date named) and leave it with no button.
    if prompt.TOP_CUSTOMERS_HINT_PATTERN.search(text):
        ai_message.report_view = {"kind": "top_customers", "summary": "Top customers"}
        ai_message.save(update_fields=["report_view"])
        return

    if not (prompt.QUERY_HINT_PATTERN.search(text) or prompt.BALANCE_HINT_PATTERN.search(text)):
        return
    date_range = prompt.extract_date_range_from_text(text)
    if date_range is None:
        return

    date_from, date_to = date_range
    summary = (
        f"Details for {date_from.isoformat()}"
        if date_from == date_to
        else f"Details from {date_from.isoformat()} to {date_to.isoformat()}"
    )
    ai_message.report_view = {
        "kind": "range",
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "summary": summary,
    }
    ai_message.save(update_fields=["report_view"])


# Same intent signal apply_safe_document_send's agent layer already reacts
# to for the doc types it can auto-compose — reused here as a deterministic
# backstop for every OTHER path that can claim a send: a draft_bill's own
# text (never routed through the agent layer at all) and any plain-text
# reply. The model is told in the prompt to say plainly when WhatsApp isn't
# connected, but a small model occasionally still writes "bhej raha hoon"
# regardless — a fact this codebase already refuses to leave to the model
# everywhere else (dates, balances, delivery status) gets the same treatment
# here: checked and, if wrong, corrected server-side, never just hoped for.
_WHATSAPP_MENTION_PATTERN = re.compile(
    r"whatsapp|واٹس ایپ|واٹساپ",
    re.IGNORECASE,
)

_NOT_CONNECTED_NOTICES = {
    "en": " (WhatsApp isn't connected for this business yet — connect it in Settings before anything can be sent.)",
    "ur": " (اس بزنس کے لیے واٹس ایپ منسلک نہیں ہے — کچھ بھی بھیجنے سے پہلے سیٹنگز میں جا کر منسلک کریں۔)",
    "roman_ur": " (Is business ke liye WhatsApp connect nahi hai — kuch bhi bhejne se pehle Settings mein ja kar connect karein.)",
}

#: What actually happened to a save_now'd bill's WhatsApp send, by
#: `queue_invoice_send`'s `reason` code (apps.documents.services.
#: queue_document_send) — English only. For roman_ur/ur this feeds
#: `_delivery_summary_note` -> `_build_execution_summary` -> the
#: response-writer step, same as every other execution fact; for a
#: non-writer language (English) `_delivery_note` below wraps it in the
#: same "(...)" aside style as `_NOT_CONNECTED_NOTICES`.
_DELIVERY_REASON_TEXT = {
    "NOT_CONNECTED": "WhatsApp isn't connected for this business yet — connect it in Settings before anything can be sent",
    "NO_PHONE": "this customer has no phone number on file, so nothing could be sent",
    "NOT_ON_PLAN": "sending on WhatsApp isn't included in the current plan",
    "QUOTA_EXCEEDED": "this month's WhatsApp sending limit has been reached",
}
_DELIVERY_REASON_FALLBACK = "the bill could not be sent on WhatsApp"


def _delivery_note(delivery, language):
    """English-only "(...)" aside for a save_now bill's real send outcome —
    used when no response-writer step runs to compose one (see
    `_delivery_summary_note` for the roman_ur/ur equivalent).

    `delivery["sent"]` from `queue_document_send` means QUEUED, not
    delivered — the actual WhatsApp attempt happens afterward in the
    `document_send` job, which alone sets `DocumentDelivery.status` to
    accepted/failed. Saying "was also sent" here claimed a completed
    delivery this codebase can't actually know yet — the same overclaim
    `_answer_delivery_status_question` exists to correct on a direct
    question, just introduced here first. In-progress language only
    ("SENDING." in prompt.py's contract instructions), same as every other
    document send in this codebase.
    """
    if delivery.get("sent"):
        return " (It's being sent to the customer on WhatsApp now.)"
    reason_text = _DELIVERY_REASON_TEXT.get(delivery.get("reason"), _DELIVERY_REASON_FALLBACK)
    return f" (The bill was saved, but not sent — {reason_text}.)"


def _delivery_summary_note(delivery):
    """English execution-summary fact for a save_now bill's real send
    outcome, folded into `_build_execution_summary`'s output so the
    response-writer composes it in the owner's own language — same pattern
    as the save_now_duplicate/save_now_failed notes above. Never left
    unsaid: the owner explicitly asked for this to be sent (that is the only
    way `delivery` is non-None at all — see `_SEND_INTENT_PATTERN`), so
    silence here would read as "it worked" by omission. See `_delivery_note`
    for why "sent" is in-progress language, not a completed-delivery claim."""
    if delivery.get("sent"):
        return "The bill is also being sent to the customer on WhatsApp now (not confirmed delivered yet)."
    reason_text = _DELIVERY_REASON_TEXT.get(delivery.get("reason"), _DELIVERY_REASON_FALLBACK)
    return f"The bill was saved, but it was NOT sent on WhatsApp — {reason_text}. Tell the owner plainly."


#: The owner directly asking whether something already went out on WhatsApp
#: ("WhatsApp m send hogya?", "kya bhej diya?", "sent?") — narrower than
#: _WHATSAPP_MENTION_PATTERN alone (which would also match the owner simply
#: asking to send something, not asking about a past send).
_DELIVERY_STATUS_QUESTION_PATTERN = re.compile(
    # WhatsApp mentioned explicitly, near a completion word.
    r"whatsapp.{0,25}\b(hogya|ho gaya|hua|gaya|diya|kar diya|sent|delivered)\b"
    r"|\b(hogya|ho gaya|hua|gaya|diya|kar diya|sent|delivered)\b.{0,25}whatsapp"
    # No "WhatsApp" this time — a bare "send hogya?"/"bhej diya?" follow-up
    # right after asking to send something reads as the same question in
    # context, and "send"/"bhej" have no other meaning in this app's domain
    # (there's nothing else a shopkeeper "sends"). Missing this made a real,
    # frustrated repeat-question ("Send ho gaya? Jaldi batao...") fall
    # through to the model's own vague deflection instead of the real
    # status lookup below.
    r"|\bsend\b.{0,15}\b(hogya|ho gaya|hua|gaya)\b|\b(hogya|ho gaya|hua|gaya)\b.{0,15}\bsend\b"
    r"|\bbhej(a|o)?\b.{0,15}\b(diya|gaya|hogya|ho gaya|hua)\b"
    r"|واٹس ایپ.{0,25}(ہوگیا|ہو گیا|ہوا|گیا|دیا)|(ہوگیا|ہو گیا|ہوا|گیا|دیا).{0,25}واٹس ایپ"
    r"|بھیج.{0,15}(دیا|گیا|ہوگیا|ہو گیا)",
    re.IGNORECASE,
)

#: Real outcome, by DocumentDelivery.status (+ a "not_connected"/"none" pair
#: for when there's no row to check at all) — see
#: `_answer_delivery_status_question`. Never says "delivered": Baileys only
#: confirms WhatsApp *accepted* the message (see DocumentDelivery's own
#: docstring on why "accepted" is this system's honest ceiling), so "sent"
#: here means exactly that, not that the customer has read it.
_DELIVERY_STATUS_TEMPLATES = {
    "not_connected": {
        "en": "WhatsApp isn't connected for this business — please reconnect it in Settings, then I can send bills again.",
        "ur": "اس بزنس کے لیے واٹس ایپ منسلک نہیں ہے — براہ کرم سیٹنگز میں دوبارہ منسلک کریں، پھر میں بل بھیج سکوں گا۔",
        "roman_ur": "Is business ke liye WhatsApp connect nahi hai — barah-e-karam Settings mein dobara connect karein, phir main bill bhej sakunga.",
    },
    "accepted": {
        "en": "Yes — it was sent and WhatsApp accepted it.",
        "ur": "جی ہاں — یہ بھیج دیا گیا اور واٹس ایپ نے قبول کر لیا۔",
        "roman_ur": "Ji haan — yeh bhej diya gaya aur WhatsApp ne accept kar liya.",
    },
    "in_progress": {
        "en": "It's still being sent — not confirmed yet, please check again in a moment.",
        "ur": "یہ ابھی بھیجا جا رہا ہے — ابھی تک تصدیق نہیں ہوئی، تھوڑی دیر بعد دوبارہ چیک کریں۔",
        "roman_ur": "Yeh abhi bhej raha hoon — abhi tak confirm nahi hua, thodi der baad dobara check karein.",
    },
    "failed": {
        "en": "No, it was NOT sent — the WhatsApp delivery failed.",
        "ur": "نہیں، یہ نہیں بھیجا گیا — واٹس ایپ ڈیلیوری ناکام ہو گئی۔",
        "roman_ur": "Nahi, yeh nahi bheja gaya — WhatsApp delivery fail ho gayi.",
    },
    "none": {
        "en": "No document has been sent yet for this business.",
        "ur": "اس بزنس کے لیے ابھی تک کوئی دستاویز نہیں بھیجی گئی۔",
        "roman_ur": "Is business ke liye abhi tak koi document nahi bheja gaya.",
    },
}

#: `DocumentDelivery.error_code` -> a business-owner-safe reason, for the
#: "failed" template above. `error_message` (the field these codes pair
#: with on the row) is Python exception text meant for logs — e.g.
#: "Document service is not reachable: HTTPConnectionPool(host='localhost',
#: port=8001): ...WinError 10061..." — and must never reach the owner
#: verbatim; codes are the stable, translatable signal.
_DELIVERY_FAILURE_REASON_TEXT = {
    "RENDER_UNAVAILABLE": {
        "en": "the document couldn't be prepared — try again in a few minutes",
        "ur": "دستاویز تیار نہیں ہو سکی — چند منٹ بعد دوبارہ کوشش کریں",
        "roman_ur": "document taiyar nahi ho saka — chand minute baad dobara koshish karein",
    },
    "RENDER_FAILED": {
        "en": "the document couldn't be prepared — try again in a few minutes",
        "ur": "دستاویز تیار نہیں ہو سکی — چند منٹ بعد دوبارہ کوشش کریں",
        "roman_ur": "document taiyar nahi ho saka — chand minute baad dobara koshish karein",
    },
    "GATEWAY_UNREACHABLE": {
        "en": "WhatsApp couldn't be reached — try again in a few minutes",
        "ur": "واٹس ایپ سے رابطہ نہیں ہو سکا — چند منٹ بعد دوبارہ کوشش کریں",
        "roman_ur": "WhatsApp se rabta nahi ho saka — chand minute baad dobara koshish karein",
    },
    "SESSION_NOT_CONNECTED": {
        "en": "WhatsApp isn't connected — please reconnect it in Settings",
        "ur": "واٹس ایپ منسلک نہیں ہے — براہ کرم سیٹنگز میں دوبارہ منسلک کریں",
        "roman_ur": "WhatsApp connect nahi hai — barah-e-karam Settings mein dobara connect karein",
    },
}
_DELIVERY_FAILURE_REASON_FALLBACK = {
    "en": "sending failed for a technical reason — please try again",
    "ur": "تکنیکی وجہ سے بھیجنا ناکام ہوا — براہ کرم دوبارہ کوشش کریں",
    "roman_ur": "technical wajah se bhejna fail hua — barah-e-karam dobara koshish karein",
}


def _answer_delivery_status_question(business, ai_message, owner_text, language):
    """Server-side, ground-truth answer to a direct "did it get sent?"
    question — real WhatsApp connection state plus the most recent
    `DocumentDelivery`'s real `status`, never the model's own guess.

    Replaces the whole reply, unconditionally, whenever the question is
    asked — not just when the model got it wrong. A wrong status claim is
    actively harmful here (the owner acts on it: assumes a customer already
    has their bill, or never notices WhatsApp needs reconnecting), so this
    is treated the same as every other money-adjacent fact in this codebase
    that the model is never trusted to state on its own — checked and
    reported, always, not hoped for.
    """
    if not _DELIVERY_STATUS_QUESTION_PATTERN.search(owner_text or ""):
        return

    if not business.gateway_session_id:
        key = "not_connected"
        detail = ""
    else:
        from apps.documents.models import DocumentDelivery

        delivery = (
            DocumentDelivery.objects.filter(business=business).order_by("-created_at").first()
        )
        if delivery is None:
            key, detail = "none", ""
        elif delivery.status == "accepted":
            key, detail = "accepted", ""
        elif delivery.status == "failed":
            key = "failed"
            reason_template = _DELIVERY_FAILURE_REASON_TEXT.get(
                delivery.error_code, _DELIVERY_FAILURE_REASON_FALLBACK
            )
            detail = f" ({reason_template.get(language, reason_template['en'])})"
        else:  # "pending" or "sending"
            key, detail = "in_progress", ""

    template = _DELIVERY_STATUS_TEMPLATES[key]
    ai_message.text = template.get(language, template["en"]) + detail
    ai_message.speech_text = (
        to_urdu_script(template["roman_ur"]) or None if language == "roman_ur" else None
    )
    ai_message.save(update_fields=["text", "speech_text"])


def _enforce_whatsapp_not_connected_notice(business, ai_message, owner_text, language):
    """If the owner's message asked for something to be sent and WhatsApp
    was never connected at all (no gateway_session_id — the one fact this
    codebase treats as fully decisive, see prompt.build_business_context),
    the reply must say so plainly. Appends rather than replaces: the draft
    itself (bill figures, document summary) is still correct and useful,
    only the sending claim needs correcting."""
    if business.gateway_session_id:
        return
    if not prompt.DOCUMENT_HINT_PATTERN.search(owner_text or ""):
        return
    current_text = ai_message.text or ""
    if _WHATSAPP_MENTION_PATTERN.search(current_text):
        # The reply already talks about WhatsApp/connection status in some
        # form — trust it rather than bolting on a second, possibly
        # redundant sentence. This only fires to fill a genuine silence.
        return

    notice = _NOT_CONNECTED_NOTICES.get(language, _NOT_CONNECTED_NOTICES["en"])
    ai_message.text = current_text + notice
    if language == "roman_ur":
        urdu_notice = to_urdu_script(_NOT_CONNECTED_NOTICES["roman_ur"].strip(" ()"))
        if urdu_notice and ai_message.speech_text:
            ai_message.speech_text = f"{ai_message.speech_text} {urdu_notice}"
    ai_message.save(update_fields=["text", "speech_text"])


def apply_safe_document_send(business, conversation, ai_message, reply_data, language):
    """Auto-executes a `draft_document` request that the agent layer can
    fully resolve on its own — the extension of `record_drafted_bill`'s
    "execute now, no tap" pattern to documents. Only reached for doc types
    the Planner composes for today (invoice/receipt/statement, see
    `apps.agent.planner._GENERATOR_FOR_DOC_TYPE`); anything else (a report,
    or a doc type it couldn't resolve) falls through untouched and keeps
    behaving exactly like the existing tap-confirm `draft_document` flow.

    Returns True if `ai_message.text`/`speech_text` were already set here to
    their final, already-localized value (a Clarification question or the
    agent layer's own execution-outcome text) — `generate_reply` uses this
    to skip running the response-writer step again over already-final text.
    Returns False if nothing was auto-composed (`plan is None`) or the
    executed plan produced no outcome text, in which case the caller must
    still supply a final reply.
    """
    from apps.agent.executor import execute_plan
    from apps.agent.planner import plan_from_reply
    from apps.agent.results import Clarification

    plan = plan_from_reply(business, conversation, ai_message, reply_data)
    if plan is None:
        return False

    if isinstance(plan, Clarification):
        # The model believed this was resolvable (it named a customer/doc
        # type) but the agent layer found it wasn't — say so plainly instead
        # of leaving whatever premature/optimistic text the model wrote.
        _overwrite_reply_text(ai_message, plan.message, language)
        return True

    outcome = execute_plan(business=business, conversation=conversation, message=ai_message, steps=plan)
    overwrote_text = False
    if outcome.text:
        _overwrite_reply_text(ai_message, outcome.text, language)
        overwrote_text = True
    if outcome.pending_delivery_id:
        from apps.documents.models import DocumentDelivery

        ai_message.pending_delivery = DocumentDelivery.objects.filter(pk=outcome.pending_delivery_id).first()
        ai_message.save(update_fields=["pending_delivery"])
    return overwrote_text


def _overwrite_reply_text(ai_message, new_text, language):
    """Replaces the model's own reply text with one the agent layer wrote
    after actually resolving/executing the request — the model wrote its
    version before knowing whether this would auto-execute. Re-derives
    speech_text the same way the main reply does, so Roman Urdu TTS never
    speaks stale content the visible text no longer matches."""
    ai_message.text = new_text
    ai_message.speech_text = (to_urdu_script(new_text) or None) if language == "roman_ur" else None
    ai_message.save(update_fields=["text", "speech_text"])
