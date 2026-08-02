import json
import logging

from apps.billing.models import Subscription
from apps.billing.services import refund_feature_usage
from apps.customers.models import Customer
from apps.image_info_extractor import matching

from . import prompt
from .groq_client import call_groq, call_groq_text
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
        # Groq, not Gemini — this is ordinary per-message chat traffic, not
        # OCR, and Gemini's free-tier quota (20 requests/day) can't carry it.
        # See call_groq_text's docstring.
        converted = call_groq_text(
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


def record_drafted_bill(business, message):
    """Records `message.draft_bill` on the ledger immediately, skipping Confirm.

    Only reached when the owner asked for it in words ("record mein save kar
    do") and the model set `draft_bill.save_now`. Confirm still exists and is
    still the default — this is the explicit-instruction shortcut, not a change
    to how ordinary drafts behave.

    Every guard the Confirm button relies on is deliberately kept, because none
    of them were about the tap: the same `build_sale_from_draft` validation, the
    same requirement that a real customer is linked, and the same audit row
    recording that the AI created this entry. Returns True only if money was
    actually written.

    Deliberately does NOT send anything on WhatsApp. The owner asked to save,
    and delivery is a separate decision — see `queue_invoice_send`'s caller.
    """
    from apps.sales import services as sales_services
    from apps.sales.business_date import to_entry_timestamp

    from .views import build_sale_from_draft

    draft = message.draft_bill or {}
    customer = Customer.objects.filter(business=business, pk=draft.get("customer_id") or 0).first()
    if not customer:
        # Nothing to attach the money to. The draft stays unconfirmed so the
        # owner can pick a customer and confirm by hand.
        logger.info("save_now skipped: draft on message %s has no matched customer", message.id)
        return False

    try:
        items, payment_received, business_date = build_sale_from_draft(draft)
    except ValueError as exc:
        logger.warning("save_now rejected on message %s: %s", message.id, exc)
        return False

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
        return False

    sales_services.log_ai_created_sale(entry=sale_entry, source_message_id=message.id)
    ChatMessage.objects.filter(pk=message.pk).update(draft_confirmed=True)
    message.draft_confirmed = True
    logger.info("save_now recorded sale %s from message %s", sale_entry.id, message.id)
    return True


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
        # Groq, not Gemini — moved off Gemini for quota reasons (see
        # call_groq_text's docstring), even though this is the one call in
        # this module Gemini was originally added FOR: Llama has a documented
        # history of writing Urdu script badly enough to be unintelligible
        # when spoken ("Mujhe samajh nahi aya" came back as "موجه سمجه نهين
        # آدا" — Arabic ه for Urdu ھ/ہ, mangled words). `has_urdu_script`
        # below cannot catch that specific failure mode: the mangled output
        # is still technically in the Urdu/Arabic Unicode block, so it looks
        # "valid" to that check. If TTS starts sounding like noise again for
        # Roman Urdu owners, this is the first place to look.
        converted = call_groq_text(
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


#: Appended when a Roman Urdu reply leaked native/Arabic script — a targeted
#: nudge for the retry, not a generic "try again".
SCRIPT_LEAK_REMINDER = (
    "\n\nIMPORTANT: your last reply's \"text\" contained Urdu script. \"text\" for Roman Urdu MUST be "
    "Latin letters only (e.g. \"Ali ka 5000 ka bill ban gaya\"). Reply again with \"text\" in Latin "
    "script only."
)


def select_model_tier(message_text, language):
    """Which model answers this turn — two Groq tiers. `needs_reasoning`
    recognises bill/edit/document intent in English, Urdu and Roman Urdu
    alike. Language then overrides intent: both Urdu variants go to the
    reasoning tier (70B) regardless of intent, since the fast model (8B)
    cannot WRITE Urdu acceptably at all — it once returned "پاپ کا جو 1 ڑڑ
    خَد 20 مۉّد" to a real owner, which is not words.

    Gemini is not used for chat replies at all — its free-tier quota (20
    requests/day) can't carry ordinary chat volume; it is reserved for
    `apps.image_info_extractor.gemini_client.extract_receipt_data` (OCR)
    only. A Roman Urdu reply that leaks script gets a same-tier retry with a
    stricter reminder instead (see `generate_reply`), not a different model.
    """
    if language in ("ur", "roman_ur"):
        return "reasoning"
    return "reasoning" if prompt.needs_reasoning(message_text) else "fast"


def _call_model(tier, messages):
    return call_groq(messages=messages, reasoning=(tier == "reasoning"))


def generate_reply(*, business, conversation, text, language=None):
    language = language or business.language
    history = _recent_history(conversation, _history_limit(business))
    messages = prompt.build_messages(business, history, text, language=language)
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

        if attempt == 0 and language == "roman_ur" and has_urdu_script(candidate.get("text") or ""):
            # Groq's Roman Urdu reply leaked native/Arabic script instead of
            # staying Latin. Retried once, same tier, with a targeted
            # reminder — no fallback to a different model (Gemini is
            # reserved for OCR only, see select_model_tier).
            logger.warning("groq roman_ur reply leaked script, retrying with a stricter reminder")
            messages = messages[:-1] + [
                {"role": "user", "content": text + SCRIPT_LEAK_REMINDER}
            ]
            continue

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

    speech_text = (reply_data.get("speech_text") or "").strip()
    reply_text = reply_data.get("text") or ""
    if language == "roman_ur" and not ai_failed:
        # ALWAYS re-derive, never keep the chat model's own speech_text. The
        # previous version only filled in an empty one — so whenever Llama did
        # emit Urdu script it was kept, and Llama's Urdu is not words:
        # "pap ka 25000 ka bill taiyar hai" came out as
        # "پَټ کا 25000 کا بل ٹئار هَ". The check was "is there any Urdu script
        # here", which mangled output passes just as easily as correct output.
        # This text is only ever spoken aloud, so it goes to the model that can
        # actually write Urdu (see to_urdu_script).
        speech_text = to_urdu_script(reply_text)

    ChatMessage.objects.create(conversation=conversation, sender="owner", text=text)
    ai_message = ChatMessage.objects.create(
        conversation=conversation,
        sender="ai",
        text=reply_data.get("text"),
        speech_text=speech_text or None,
        draft_bill=reply_data.get("draft_bill"),
        document_ready=reply_data.get("document_ready"),
        draft_action=reply_data.get("draft_action"),
        draft_document=reply_data.get("draft_document"),
        # Marked so this apology isn't replayed into later prompts as if it were
        # a real assistant turn — feeding "sorry, I couldn't process that" back
        # as context teaches the model to produce more of the same.
        is_error_fallback=ai_failed,
    )

    # Recorded only when the owner asked for it in words. Done after the message
    # exists so the audit row can point at the message that caused the entry.
    draft = reply_data.get("draft_bill") or {}
    if draft.get("save_now") and not ai_failed:
        if not record_drafted_bill(business, ai_message):
            # Say so rather than leaving the owner believing it is on the
            # ledger — the reply text already claims it was saved, because the
            # model was told to only set save_now when it means it.
            ai_message.text = (
                f"{ai_message.text}\n\n(I could not record it automatically — "
                "please check the draft and confirm it.)"
            )
            ai_message.speech_text = None
            ai_message.save(update_fields=["text", "speech_text"])

    if not ai_failed:
        apply_safe_document_send(business, conversation, ai_message, reply_data, language)

    return ai_message


def apply_safe_document_send(business, conversation, ai_message, reply_data, language):
    """Auto-executes a `draft_document` request that the agent layer can
    fully resolve on its own — the extension of `record_drafted_bill`'s
    "execute now, no tap" pattern to documents. Only reached for doc types
    the Planner composes for today (invoice/receipt/statement, see
    `apps.agent.planner._GENERATOR_FOR_DOC_TYPE`); anything else (a report,
    or a doc type it couldn't resolve) falls through untouched and keeps
    behaving exactly like the existing tap-confirm `draft_document` flow.
    """
    from apps.agent.executor import execute_plan
    from apps.agent.planner import plan_from_reply
    from apps.agent.results import Clarification

    plan = plan_from_reply(business, conversation, ai_message, reply_data)
    if plan is None:
        return

    if isinstance(plan, Clarification):
        # The model believed this was resolvable (it named a customer/doc
        # type) but the agent layer found it wasn't — say so plainly instead
        # of leaving whatever premature/optimistic text the model wrote.
        _overwrite_reply_text(ai_message, plan.message, language)
        return

    outcome = execute_plan(business=business, conversation=conversation, message=ai_message, steps=plan)
    if outcome.text:
        _overwrite_reply_text(ai_message, outcome.text, language)
    if outcome.pending_delivery_id:
        from apps.documents.models import DocumentDelivery

        ai_message.pending_delivery = DocumentDelivery.objects.filter(pk=outcome.pending_delivery_id).first()
        ai_message.save(update_fields=["pending_delivery"])


def _overwrite_reply_text(ai_message, new_text, language):
    """Replaces the model's own reply text with one the agent layer wrote
    after actually resolving/executing the request — the model wrote its
    version before knowing whether this would auto-execute. Re-derives
    speech_text the same way the main reply does, so Roman Urdu TTS never
    speaks stale content the visible text no longer matches."""
    ai_message.text = new_text
    ai_message.speech_text = (to_urdu_script(new_text) or None) if language == "roman_ur" else None
    ai_message.save(update_fields=["text", "speech_text"])
