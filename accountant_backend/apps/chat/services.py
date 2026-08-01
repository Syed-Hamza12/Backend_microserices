import json
import logging

from apps.billing.models import Subscription
from apps.billing.services import refund_feature_usage
from apps.customers.models import Customer
from apps.image_info_extractor import gemini_client, matching

from . import prompt
from .groq_client import call_groq
from .models import ChatMessage
from .serializers import AiReplySerializer

logger = logging.getLogger(__name__)

FALLBACK_REPLY = {
    "text": "Sorry, I couldn't process that right now — please try again.",
    "speech_text": None,
    "draft_bill": None,
    "document_ready": None,
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
        # Gemini for the same reason as [to_urdu_script]: this is the other
        # direction of the same round trip, and the owner's own dictated words
        # are what get mangled here.
        converted = gemini_client.generate_text(
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
        # Gemini, not Groq. Llama writes Urdu script badly enough to be
        # unintelligible when spoken: "Mujhe samajh nahi aya" came back as
        # "موجه سمجه نهين آدا" (Arabic ه for Urdu ھ/ہ, mangled words), and the
        # ur-PK voice reads that out as noise. Gemini returns
        # "مجھے سمجھ نہیں آیا" and even renders English words speakably
        # ("please" -> "پلیز"), which matters because this text exists only to
        # be spoken.
        converted = gemini_client.generate_text(
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


def generate_reply(*, business, conversation, text, language=None):
    language = language or business.language
    history = _recent_history(conversation, _history_limit(business))
    messages = prompt.build_messages(business, history, text, language=language)
    # `needs_reasoning` is the real router and now recognises bill/edit intent in
    # Urdu and Roman Urdu too (it was English-only, so every Urdu request was
    # misrouted to the fast model). The language check below is a second layer
    # for a different problem: the fast model cannot WRITE Urdu acceptably — it
    # returned "پاپ کا جو 1 ڑڑ خَد 20 مۉّد" to a real owner, which is not words.
    # So even genuinely simple messages go to the larger model when the reply
    # will be in Urdu or Roman Urdu. Drop this clause if the fast model's Urdu
    # ever becomes good enough; the intent routing above stands on its own.
    reasoning = prompt.needs_reasoning(text) or language in ("ur", "roman_ur")

    reply_data = None
    for attempt in range(2):
        try:
            raw = call_groq(messages=messages, reasoning=reasoning)
            reply_data = _parse_and_validate(raw)
            break
        except Exception as exc:  # noqa: BLE001 - covers Groq errors + bad-shape JSON
            logger.warning("chat reply attempt %s failed: %s", attempt, exc)
            if attempt == 0:
                messages = messages[:-1] + [
                    {"role": "user", "content": text + STRICTER_REMINDER}
                ]

    ai_failed = reply_data is None
    if ai_failed:
        reply_data = FALLBACK_REPLY
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

    return ai_message
