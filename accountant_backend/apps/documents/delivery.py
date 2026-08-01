"""Document send orchestration.

The whole flow, and where each responsibility lives:

    business data (Django, source of truth)
        -> render_document()      FastAPI turns the payload into bytes
        -> gateway_client         bytes uploaded straight to WhatsApp
        -> bytes go out of scope  nothing was ever written to disk
        -> DocumentDelivery       audit row: what went where, and did it arrive

Runs inside the background worker because rendering plus the Gateway's
deliberate anti-ban send pacing takes several seconds — too long to hold a
mobile HTTP request open, and a send that fails mid-request would leave no
record of having been attempted.
"""

import logging

from django.utils import timezone

from apps.billing.services import refund_feature_usage
from apps.notifications.services import create_notification
from apps.whatsapp import gateway_client
from apps.whatsapp.gateway_client import GatewayError

from .models import DocumentDelivery
from .services import DocumentError, build_payload_for, render_document

logger = logging.getLogger(__name__)

# A short line accompanying the file, so the customer sees context rather than a
# bare attachment landing in their chat.
CAPTIONS = {
    "invoice": "Invoice from {business}",
    "receipt": "Payment receipt from {business}",
    "statement": "Account statement from {business}",
    "report": "Business report from {business}",
}


def _fail(delivery, code, message):
    # The send never went out, so the quota slot claimed when it was queued is
    # given back. Charging a business for a document the customer never
    # received is exactly the kind of quiet unfairness that costs trust.
    refund_feature_usage(delivery.business, "whatsapp_send")
    delivery.status = "failed"
    delivery.error_code = code
    delivery.error_message = message
    delivery.save(update_fields=["status", "error_code", "error_message"])
    create_notification(
        delivery.business,
        "document_failed",
        payload={
            "delivery_id": delivery.id,
            "doc_type": delivery.doc_type,
            "code": code,
            "message": message,
        },
    )
    return {"status": "failed", "delivery_id": delivery.id, "error": {"code": code, "message": message}}


def handle_document_send_job(job_task):
    """Worker entry point for type="document_send" JobTasks."""
    business = job_task.business
    delivery = DocumentDelivery.objects.get(pk=job_task.payload["delivery_id"], business=business)

    # Guard against a job being processed twice: only a pending delivery may be
    # picked up, so a duplicate run can never send a customer the same document
    # a second time.
    claimed = DocumentDelivery.objects.filter(pk=delivery.pk, status="pending").update(status="sending")
    if not claimed:
        logger.warning("delivery %s was already claimed; skipping", delivery.pk)
        return {"status": delivery.status, "delivery_id": delivery.id, "skipped": True}
    delivery.status = "sending"

    params = delivery.parameters or {}
    try:
        payload, _entry, _customer = build_payload_for(
            business,
            doc_type=delivery.doc_type,
            target_id=params.get("target_id"),
            customer_id=params.get("customer_id"),
            date_from=params.get("date_from"),
            date_to=params.get("date_to"),
        )
        content, delivered_format = render_document(
            doc_type=delivery.doc_type,
            output_format=delivery.requested_format,
            business_id=business.id,
            payload=payload,
        )
    except DocumentError as exc:
        return _fail(delivery, exc.code, exc.message)

    delivery.delivered_format = delivered_format
    delivery.byte_size = len(content)
    delivery.save(update_fields=["delivered_format", "byte_size"])

    extension = "png" if delivered_format == "image" else "pdf"
    file_name = f"{delivery.doc_type}_{delivery.id}.{extension}"
    caption = CAPTIONS.get(delivery.doc_type, "{business}").format(business=business.business_name)

    try:
        gateway_client.send_media(
            business.gateway_session_id,
            delivery.to_phone,
            kind="image" if delivered_format == "image" else "document",
            file_name=file_name,
            content=content,
            caption=caption,
        )
    except GatewayError as exc:
        return _fail(delivery, exc.code, exc.message)

    # `content` is never persisted — it goes out of scope here and the file
    # ceases to exist. Regeneration from the stored business data is the
    # supported path if the document is needed again.
    # "accepted", not "sent": WhatsApp has taken the message. Whether it
    # reached the customer's phone is not something Baileys tells us.
    delivery.status = "accepted"
    delivery.accepted_at = timezone.now()
    delivery.save(update_fields=["status", "accepted_at"])

    create_notification(
        business,
        "invoice_sent",
        payload={
            "delivery_id": delivery.id,
            "doc_type": delivery.doc_type,
            "format": delivered_format,
            "to": delivery.to_phone,
        },
    )
    logger.info(
        "document delivered: business=%s type=%s format=%s bytes=%s",
        business.id,
        delivery.doc_type,
        delivered_format,
        delivery.byte_size,
    )

    return {
        "status": "accepted",
        "delivery_id": delivery.id,
        "doc_type": delivery.doc_type,
        "format": delivered_format,
        "to": delivery.to_phone,
        "accepted_at": delivery.accepted_at.isoformat(),
    }
