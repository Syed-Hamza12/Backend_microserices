import logging
import mimetypes
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings

from apps.billing.services import refund_feature_usage
from apps.chat.models import ChatMessage, Conversation
from apps.chat.serializers import ChatMessageSerializer
from apps.sales import business_date

from . import gemini_client, matching
from .clarification import build_clarification_reply
from .models import ExtractionJob

logger = logging.getLogger(__name__)

FALLBACK_TEXT = "Sorry, I couldn't process that right now — please try again."

MAX_EXTRACTED_AMOUNT = Decimal("9999999999.99")
MAX_EXTRACTED_ITEMS = 50


def _read_image(source_image_url):
    relative = source_image_url
    if relative.startswith(settings.MEDIA_URL):
        relative = relative[len(settings.MEDIA_URL):]

    media_root = Path(settings.MEDIA_ROOT).resolve()
    path = (media_root / relative).resolve()

    # Containment check: `relative` comes from a stored URL, and joining an
    # unchecked relative path onto MEDIA_ROOT is how a traversal ("../../..")
    # turns into reading an arbitrary server file and shipping it to a
    # third-party API. Today the value is server-generated, so this is cheap
    # insurance — the point is that it stays closed if the URL ever becomes
    # client-influenced.
    if not path.is_relative_to(media_root):
        raise ValueError("Refusing to read an image from outside MEDIA_ROOT.")

    mime_type = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    return path.read_bytes(), mime_type


def _parse_amount(raw):
    """OCR amount -> Decimal, or None if it isn't usable.

    Vision output is free-form: "1,200", "Rs 1200", None, or a plain number.
    This was previously `float(extracted["amount"])` sitting *outside* the job's
    try block, so anything non-numeric raised straight out of the handler — the
    job was marked failed and the owner got no reply at all, just a spinner that
    never resolved.
    """
    if raw is None:
        return None
    text = str(raw).strip().replace(",", "")

    # Reject anything negative *before* stripping punctuation. Filtering to
    # digits-and-dot alone silently turns "-5" into "5" — a negative reading
    # would become a positive charge on the customer's account.
    if "-" in text or "−" in text:
        return None

    cleaned = "".join(ch for ch in text if ch.isdigit() or ch == ".")
    if not cleaned or cleaned == ".":
        return None
    try:
        amount = Decimal(cleaned).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None
    if amount <= 0 or amount > MAX_EXTRACTED_AMOUNT:
        return None
    return amount


def _parse_bill_date(raw):
    """The date read off the bill, as a `YYYY-MM-DD` string the draft can carry.

    Returns None (meaning "today") when the reading is unusable. Resolving it
    here rather than leaving it to confirm-time matters: a misread year is the
    most common OCR slip on handwriting, and `resolve` rejects anything more
    than a year out. Stored unchecked, that bad string would sit in the draft
    and fail *every* confirm attempt with a date error the owner cannot clear
    without editing the draft — a stuck bill. Falling back to today keeps the
    bill confirmable, and the date is on screen for them to correct.
    """
    if not raw:
        return None
    try:
        resolved = business_date.resolve(str(raw).strip())
    except business_date.BusinessDateError:
        return None
    return resolved.isoformat() if resolved else None


def _parse_items(raw_items):
    """Line items from OCR, keeping only rows that are fully readable.

    quantity/rate are stored as JSON *numbers*, matching what the chat path
    writes. They used to be stringified Decimals, so a photo-derived draft and
    a typed one had different types in the same field — and the app, which
    reads them as `num`, threw "type 'String' is not a subtype of type 'num?'"
    and failed to restore the entire chat history. Same reason the reconcile
    below keeps Decimals internally and converts only at the end: float
    arithmetic must not decide whether the items add up.
    """
    if not isinstance(raw_items, list):
        return []
    items = []
    for raw in raw_items[:MAX_EXTRACTED_ITEMS]:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("item_name") or "").strip()[:255]
        quantity = _parse_amount(raw.get("quantity"))
        rate = _parse_amount(raw.get("rate"))
        if name and quantity and rate:
            items.append({"item_name": name, "quantity": quantity, "rate": rate})
    return items


def handle_image_extract_job(job_task):
    """Called by the jobs worker loop for type="image_extract" JobTasks. Calls Gemini
    directly from Django (see ai_automation_layer.md Section 3) rather than proxying through
    FastAPI — FastAPI's own /vision/extract stays available for a future local-model swap."""
    extraction_job = ExtractionJob.objects.get(job_task=job_task)
    business = job_task.business

    try:
        conversation = Conversation.objects.get(pk=job_task.payload["conversation_id"], business=business)
    except Conversation.DoesNotExist:
        # The owner cleared their chat history while this job was queued. There
        # is nowhere to post the reply, so record it and stop — better than the
        # worker raising DoesNotExist on a job that can never succeed.
        extraction_job.status = "failed"
        extraction_job.save(update_fields=["status"])
        refund_feature_usage(business, "image_extraction")
        logger.info("image extract job %s abandoned: conversation was deleted", job_task.id)
        return {"status": "abandoned", "reason": "conversation_deleted"}

    try:
        image_bytes, mime_type = _read_image(extraction_job.source_image_url)
        extracted = gemini_client.extract_receipt_data(image_bytes, mime_type)
    except Exception as exc:  # noqa: BLE001 - Gemini unreachable/not configured must degrade gracefully
        extraction_job.status = "failed"
        extraction_job.save(update_fields=["status"])
        # The upload endpoint claimed an image_extraction slot before queueing
        # this job. The extraction never happened, so the owner shouldn't be
        # billed a month's quota for it — same reasoning as the chat path.
        refund_feature_usage(business, "image_extraction")
        message = ChatMessage.objects.create(conversation=conversation, sender="ai", text=FALLBACK_TEXT)
        # `message` included here (not just "status"/"error") so the mobile app can render the AI's
        # reply straight from this one poll response — there is no separate "list conversation
        # messages" endpoint, this job result is the only place the client learns what was said.
        return {"status": "failed", "error": str(exc), "message": ChatMessageSerializer(message).data}

    if not isinstance(extracted, dict):
        extracted = {}

    extraction_job.extracted_data = extracted
    amount = _parse_amount(extracted.get("amount"))
    customer, candidates = matching.find_matching_customer(business, extracted.get("customer_name"))
    extraction_job.resolved_customer = customer

    # Only a missing AMOUNT blocks building a draft. An unreadable customer or
    # date used to block it too, and that lost the bill: everything the photo
    # said (25,000 / "20 mm black" / 20000 x 1.25) stayed in this job row while
    # the conversation got a bare "which customer is this for?" and no draft.
    # The chat model answering that follow-up had never seen the bill, so it
    # invented the figures — which reads as "the AI extracted it wrong" when
    # the extraction was in fact perfect.
    #
    # Now the draft is built with whatever the photo gave, and the unknown parts
    # are asked about *on top of it*: the customer is left unlinked for the
    # owner to pick on the Edit Draft screen, and a missing date means today.
    missing_fields = []
    if amount is None:
        missing_fields.append("amount")

    if missing_fields:
        extraction_job.status = "needs_clarification"
        reply = build_clarification_reply(business, extracted, missing_fields, candidates=candidates)
        message = ChatMessage.objects.create(
            conversation=conversation,
            sender="ai",
            text=reply.get("text"),
            speech_text=reply.get("speech_text"),
        )
    else:
        # A draft is attached either way; the status records whether anything
        # still needs the owner's input, which is what the admin/audit view and
        # the job history are read for.
        extraction_job.status = "resolved" if customer else "needs_clarification"
        items = _parse_items(extracted.get("line_items"))
        # Only carry items through if they reconcile with the total read off the
        # bill. Two figures that disagree is a question for the owner, not
        # something to quietly reconcile on their behalf.
        itemised_total = sum(i["quantity"] * i["rate"] for i in items) if items else None
        if itemised_total is None or itemised_total.quantize(Decimal("0.01")) != amount:
            items = []

        # Decimals are exact for the reconcile above but aren't JSON-serialisable;
        # floats are what this field has always held on the wire.
        items = [
            {
                "item_name": i["item_name"],
                "quantity": float(i["quantity"]),
                "rate": float(i["rate"]),
            }
            for i in items
        ]

        # Any previous balance written on the bill is deliberately ignored: the
        # ledger is the authority on what this customer already owes, and
        # trusting the paper figure would double-count the moment the two
        # disagree. It is extracted only so the model has somewhere to put it
        # other than `amount` — see gemini_client.EXTRACTION_PROMPT.
        payment_received = _parse_amount(extracted.get("amount_received")) or Decimal("0")
        if payment_received > amount:
            # A reading that says more was paid than was sold fails validation
            # at confirm time anyway; drop it rather than hand the owner a
            # draft they cannot confirm.
            payment_received = Decimal("0")

        draft_bill = {
            # Unlinked when the photo's name matched nobody. The card and the
            # Edit Draft screen both handle that: the owner picks the customer
            # there, and the confirm endpoint refuses until one is set — so an
            # unmatched name can never quietly land on the wrong ledger.
            "customer_id": str(customer.id) if customer else None,
            "customer_name_guess": None if customer else (extracted.get("customer_name") or None),
            # Near-miss matches the owner can pick from on the Edit Draft screen
            # instead of retyping a name — additive field, ignored by older clients.
            "customer_candidates": (
                [{"id": str(c.id), "name": c.name} for c in candidates] if not customer else []
            ),
            # Shown on the draft card and the Edit Draft screen — without it a
            # matched draft renders with a blank customer.
            "customer_name": customer.name if customer else "",
            "previous_balance": float(customer.current_balance) if customer else 0.0,
            "total_amount": float(amount),
            "payment_received": float(payment_received),
            "items": items,
            # The date written on the bill, not today.
            "date": _parse_bill_date(extracted.get("date")),
        }
        # The draft carries the figures, so the question is only ever about the
        # one thing the photo could not settle — and it is asked with the bill
        # already on screen, not instead of it.
        if customer:
            reply_text = (
                f"I read a bill for {customer.name}: {business.currency_code} {amount}. "
                "Please check the details and confirm."
            )
        else:
            read_as = (extracted.get("customer_name") or "").strip()
            if candidates:
                # The name on the bill was close to one or more existing customers but
                # not decisively — naming them turns a guess into a one-tap answer
                # instead of leaving the owner to retype a name from scratch.
                names = ", ".join(c.name for c in candidates)
                reply_text = (
                    f"I read a bill for {business.currency_code} {amount}"
                    + (f' (the name looks like "{read_as}")' if read_as else "")
                    + f". Is this for {names}? Tap Edit on the draft to pick them, then confirm."
                )
            else:
                reply_text = (
                    f"I read a bill for {business.currency_code} {amount}"
                    + (f' (the name looks like "{read_as}")' if read_as else "")
                    + ". Which customer is this for? Tap Edit on the draft to pick them, then confirm."
                )
        message = ChatMessage.objects.create(
            conversation=conversation,
            sender="ai",
            text=reply_text,
            draft_bill=draft_bill,
        )

    extraction_job.save()
    return {"status": extraction_job.status, "message": ChatMessageSerializer(message).data}
