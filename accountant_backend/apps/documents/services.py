from decimal import ROUND_HALF_UP, Decimal

import requests
from django.conf import settings
from django.db.models import Sum
from django.utils import timezone
from django.utils.dateparse import parse_date

from apps.customers.models import Customer
from apps.sales.models import ActivityEntry



# Presentation helpers.
#
# Django formats every value that appears on a document, because Django owns the
# figures — the renderer only lays out what it is handed, and giving it raw
# repr() output puts that straight in front of a customer. Two defects this
# fixes, both seen on a real rendered bill: dates arriving as
# "2026-07-30T18:47:44.786536+00:00", and line amounts as "3000.0000" because
# Decimal multiplication widens the scale (2.00 * 1500.00 -> 3000.0000).

DISPLAY_DATE_FORMAT = "%d %b %Y"


def _money(value):
    """Money as a plain 2-decimal string."""
    return str(Decimal(value or 0).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _quantity(value):
    """Quantity without pointless trailing zeros: 2.00 -> "2", 2.50 -> "2.5"."""
    quantised = Decimal(value or 0).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    normalised = quantised.normalize()
    # normalize() turns 200 into 2E+2; expand it back to plain digits.
    if normalised == normalised.to_integral_value():
        normalised = normalised.to_integral_value()
    return f"{normalised:f}"


def _display_date(value):
    """A date a customer can read, in local time."""
    if value is None:
        return ""
    if hasattr(value, "tzinfo") and value.tzinfo is not None:
        value = timezone.localtime(value)
    return value.strftime(DISPLAY_DATE_FORMAT)


def _parse_range(date_from, date_to):
    start = None
    end = None
    if date_from:
        d = parse_date(date_from)
        if d:
            start = timezone.make_aware(timezone.datetime(d.year, d.month, d.day))
    if date_to:
        d = parse_date(date_to)
        if d:
            end = timezone.make_aware(timezone.datetime(d.year, d.month, d.day, 23, 59, 59))
    return start, end


def build_invoice_payload(entry: ActivityEntry):
    business = entry.business
    customer = entry.customer
    line_items = [
        {
            "item_name": li.item_name,
            "quantity": _quantity(li.quantity),
            "rate": _money(li.rate),
            "amount": _money(li.amount),
        }
        for li in entry.line_items.all()
    ]
    linked_payment = None
    if entry.sale_group_id:
        # Scoped to the business, like the other sale_group_id lookups — this
        # figure is printed on an invoice that gets sent to a customer, so a
        # cross-tenant row reaching it would be a data leak, not just a bug.
        linked_payment = ActivityEntry.objects.filter(
            business=business, sale_group_id=entry.sale_group_id, type="payment"
        ).exclude(pk=entry.pk).first()

    return {
        "business_name": business.business_name,
        "currency_code": business.currency_code,
        "invoice_no": entry.id,
        "customer_name": customer.name,
        "customer_phone": customer.phone,
        "date": _display_date(entry.timestamp),
        "line_items": line_items,
        "subtotal": _money(entry.amount),
        "amount_received": _money(linked_payment.amount) if linked_payment else "0.00",
        # The balance AFTER the linked payment, not after the sale alone.
        #
        # `record_sale` timestamps the payment 1ms after the sale, so the sale
        # row's own balance_after predates it. Using that value produced an
        # invoice whose arithmetic contradicted itself — "Subtotal 3720,
        # Received 1000, Balance 3720" — overstating what the customer owed on
        # a document they were about to be sent.
        "balance_after": _money(linked_payment.balance_after if linked_payment else entry.balance_after),
    }


def build_receipt_payload(entry: ActivityEntry):
    business = entry.business
    customer = entry.customer
    return {
        "business_name": business.business_name,
        "currency_code": business.currency_code,
        "receipt_no": entry.id,
        "customer_name": customer.name,
        "customer_phone": customer.phone,
        "date": _display_date(entry.timestamp),
        "amount": _money(entry.amount),
        "payment_method": (entry.payment_method or "").title(),
        "balance_after": _money(entry.balance_after),
    }


def build_statement_payload(business, customer, date_from, date_to):
    start, end = _parse_range(date_from, date_to)
    entries = ActivityEntry.objects.filter(business=business, customer=customer).order_by("timestamp", "id")
    if start:
        entries = entries.filter(timestamp__gte=start)
    if end:
        entries = entries.filter(timestamp__lte=end)

    rows = [
        {
            "date": _display_date(e.timestamp),
            "type": e.type.title(),
            "amount": _money(e.amount),
            "balance_after": _money(e.balance_after),
            "note": e.note or "",
        }
        for e in entries
    ]

    return {
        "business_name": business.business_name,
        "currency_code": business.currency_code,
        "customer_name": customer.name,
        "customer_phone": customer.phone,
        "date_from": date_from or "—",
        "date_to": date_to or "—",
        "current_balance": _money(customer.current_balance),
        "rows": rows,
    }


def build_report_payload(business, date_from, date_to):
    start, end = _parse_range(date_from, date_to)
    entries = ActivityEntry.objects.filter(business=business).order_by("timestamp", "id")
    if start:
        entries = entries.filter(timestamp__gte=start)
    if end:
        entries = entries.filter(timestamp__lte=end)

    total_sales = entries.filter(type="sale").aggregate(total=Sum("amount"))["total"] or 0
    total_payments = entries.filter(type="payment").aggregate(total=Sum("amount"))["total"] or 0

    per_customer = {}
    for e in entries.select_related("customer"):
        row = per_customer.setdefault(
            e.customer_id, {"customer_name": e.customer.name, "sales": 0, "payments": 0}
        )
        if e.type == "sale":
            row["sales"] += e.amount
        else:
            row["payments"] += e.amount

    customer_rows = [
        {"customer_name": row["customer_name"], "sales": _money(row["sales"]), "payments": _money(row["payments"])}
        for row in per_customer.values()
    ]

    return {
        "business_name": business.business_name,
        "currency_code": business.currency_code,
        "date_from": date_from or "—",
        "date_to": date_to or "—",
        "total_sales": _money(total_sales),
        "total_payments": _money(total_payments),
        "customer_rows": customer_rows,
    }


class DocumentError(Exception):
    """A document could not be built or rendered. Carries a client-safe code."""

    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def build_payload_for(business, *, doc_type, target_id=None, customer_id=None, date_from=None, date_to=None):
    """Assembles the figures for a document straight from the database.

    Every number on a document originates here. FastAPI only lays out what it is
    given, and the mobile app renders nothing at all — one place computes the
    money, so an invoice, its image, and the ledger can never disagree.

    Returns `(payload, related_entry, customer)`.
    """
    if doc_type in ("invoice", "receipt"):
        expected_type = "sale" if doc_type == "invoice" else "payment"
        try:
            entry = ActivityEntry.objects.get(business=business, pk=target_id, type=expected_type)
        except (ActivityEntry.DoesNotExist, ValueError, TypeError):
            raise DocumentError("NOT_FOUND", "That record could not be found.")
        payload = build_invoice_payload(entry) if doc_type == "invoice" else build_receipt_payload(entry)
        return payload, entry, entry.customer

    if doc_type == "statement":
        try:
            customer = Customer.objects.get(business=business, pk=customer_id)
        except (Customer.DoesNotExist, ValueError, TypeError):
            raise DocumentError("NOT_FOUND", "Customer not found.")
        return build_statement_payload(business, customer, date_from, date_to), None, customer

    if doc_type == "report":
        return build_report_payload(business, date_from, date_to), None, None

    raise DocumentError("UNSUPPORTED_DOC_TYPE", f"Unsupported doc_type: {doc_type}")


def render_document(*, doc_type, output_format, business_id, payload):
    """Renders a document via FastAPI and returns `(bytes, delivered_format)`.

    The bytes are returned, never written anywhere. A generated document is a
    transient artifact: it exists in memory long enough to be sent, then it is
    gone. The database is the permanent record, and any document can be rebuilt
    from it on demand.
    """
    try:
        response = requests.post(
            f"{settings.FASTAPI_BASE_URL}/documents/render",
            json={
                "doc_type": doc_type,
                "format": output_format,
                "business_id": business_id,
                "payload": payload,
            },
            headers={"X-Internal-Key": settings.FASTAPI_INTERNAL_KEY},
            timeout=60,
        )
    except requests.RequestException as exc:
        raise DocumentError("RENDER_UNAVAILABLE", f"Document service is not reachable: {exc}")

    if response.status_code >= 400:
        try:
            error = response.json()["detail"]["error"]
            raise DocumentError(error["code"], error["message"])
        except (ValueError, KeyError, TypeError):
            raise DocumentError("RENDER_FAILED", f"Document service returned {response.status_code}.")

    # Trust the header over the request: the renderer substitutes PDF for an
    # image that would be too long to read, and the delivery record must show
    # what was actually produced.
    delivered_format = response.headers.get("X-Document-Format", output_format)
    return response.content, delivered_format


def queue_invoice_send(business, entry, customer):
    """Queues an invoice for `entry` to `customer`'s WhatsApp. Returns a dict
    describing what happened, and never raises.

    Split out of `SendDocumentView` so the chat confirm can reuse the one send
    pipeline rather than growing a second, subtly different one.

    Every "can't send" case here is a *reason*, not an error: the caller has
    already recorded the sale on the ledger, and a bill that is saved but not
    delivered must never look like a bill that failed to save. The owner is
    told the money is recorded and why the message didn't go, so they can send
    it by hand.
    """
    # Imported here rather than at module scope: apps.billing imports back into
    # documents for its usage reporting, and a top-level import closes that loop.
    from apps.billing.exceptions import FeatureNotOnPlan, UsageCapExceeded
    from apps.billing.services import enforce_feature_gate
    from apps.jobs.dispatch import enqueue

    from .models import DocumentDelivery

    if not business.gateway_session_id:
        return {"sent": False, "reason": "NOT_CONNECTED"}
    if not customer or not customer.phone:
        return {"sent": False, "reason": "NO_PHONE"}

    try:
        enforce_feature_gate(business, "whatsapp_send")
    except FeatureNotOnPlan:
        return {"sent": False, "reason": "NOT_ON_PLAN"}
    except UsageCapExceeded:
        return {"sent": False, "reason": "QUOTA_EXCEEDED"}

    delivery = DocumentDelivery.objects.create(
        business=business,
        customer=customer,
        doc_type="invoice",
        # Bills go as an image so the customer reads them inline in WhatsApp
        # without downloading anything — same default as the manual send.
        requested_format="image",
        to_phone=customer.phone,
        related_entry=entry,
        parameters={
            "target_id": entry.id,
            "customer_id": customer.id,
            "date_from": None,
            "date_to": None,
        },
    )
    job = enqueue(business=business, type="document_send", payload={"delivery_id": delivery.id})
    delivery.job_task = job
    delivery.save(update_fields=["job_task"])
    return {"sent": True, "delivery_id": delivery.id, "job_id": job.id}
