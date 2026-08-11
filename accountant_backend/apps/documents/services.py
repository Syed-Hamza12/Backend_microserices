import logging
from decimal import ROUND_HALF_UP, Decimal

import requests
from django.conf import settings
from django.db.models import Sum
from django.utils import timezone
from django.utils.dateparse import parse_date

from apps.customers.models import Customer
from apps.sales.business_date import BusinessDateError, resolve as resolve_business_date
from apps.sales.models import ActivityEntry

logger = logging.getLogger(__name__)

ALL_DOC_TYPES = {"invoice", "receipt", "statement", "report"}

# Which formats each document type can be produced in. Mirrors FastAPI's
# /documents/formats so the app can offer only workable choices rather than
# discovering a limitation via an error after the owner has tapped Send.
#
# Statement/report gained "image" as an explicit-request-only option: the
# owner may ask for one as an image, and the renderer's own over-length
# fallback (see `render_document`'s `X-Document-Format` handling) substitutes
# PDF automatically if it doesn't fit — the same mechanism that already
# governs invoice/receipt. DEFAULT_FORMAT is unchanged for these two, so
# nothing changes unless the owner explicitly asks for an image.
SUPPORTED_FORMATS = {
    "invoice": ["image", "pdf"],
    "receipt": ["image", "pdf"],
    "statement": ["pdf", "image"],
    "report": ["pdf", "image"],
}

DEFAULT_FORMAT = {
    # Bills default to an image: customers read them inline in WhatsApp without
    # downloading anything. Statements and reports are multi-page by nature.
    "invoice": "image",
    "receipt": "image",
    "statement": "pdf",
    "report": "pdf",
}


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
    """Money as a plain 2-decimal string.

    Deliberately not comma-grouped: callers (including tests) round-trip this
    through `Decimal()` to check document arithmetic. Comma grouping for
    display happens in the template via the `commas` Jinja filter instead.
    """
    return str(Decimal(value or 0).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


_ONES = [
    "", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
    "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen",
    "Seventeen", "Eighteen", "Nineteen",
]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]
_SCALES = [(1_000_000_000, "Billion"), (1_000_000, "Million"), (1_000, "Thousand"), (100, "Hundred")]


def _int_to_words(n):
    if n == 0:
        return "Zero"
    words = []
    for value, name in _SCALES:
        if n >= value:
            count, n = divmod(n, value)
            words.append(f"{_int_to_words(count)} {name}")
    if n < 20:
        words.append(_ONES[n])
    elif n < 100:
        tens, ones = divmod(n, 10)
        words.append(f"{_TENS[tens]} {_ONES[ones]}".strip())
    return " ".join(w for w in words if w)


def _amount_in_words(value, currency_word="Rupees"):
    """A money value spelled out for the "Amount in Words" line, e.g.
    128525.00 -> "One Hundred Twenty Eight Thousand Five Hundred Twenty Five Rupees Only".
    """
    whole = int(Decimal(value or 0).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return f"{_int_to_words(whole)} {currency_word} Only"


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

    amount_received = linked_payment.amount if linked_payment else Decimal(0)
    balance_after = linked_payment.balance_after if linked_payment else entry.balance_after
    # What the customer owed before this invoice's sale (and its linked
    # payment, if any) were applied — printed as "Previous Balance" so the
    # final Total line reads as an auditable sum, not a number pulled from
    # nowhere: Previous Balance + Current Invoice Total - Received = Total.
    previous_balance = Decimal(balance_after) - Decimal(entry.amount) + Decimal(amount_received)

    return {
        "business_name": business.business_name,
        "business_address": business.address,
        "business_phone": business.phone,
        "business_email": business.email,
        "currency_code": business.currency_code,
        "invoice_no": entry.id,
        "customer_name": customer.name,
        "customer_phone": customer.phone,
        "date": _display_date(entry.timestamp),
        "due_date": _display_date(entry.timestamp + timezone.timedelta(days=14)),
        "line_items": line_items,
        "previous_balance": _money(previous_balance),
        "subtotal": _money(entry.amount),
        "amount_received": _money(amount_received),
        # The balance AFTER the linked payment, not after the sale alone.
        #
        # `record_sale` timestamps the payment 1ms after the sale, so the sale
        # row's own balance_after predates it. Using that value produced an
        # invoice whose arithmetic contradicted itself — "Subtotal 3720,
        # Received 1000, Balance 3720" — overstating what the customer owed on
        # a document they were about to be sent.
        "balance_after": _money(balance_after),
        "amount_in_words": _amount_in_words(balance_after),
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
    entries = (
        ActivityEntry.objects.filter(business=business, customer=customer)
        .order_by("timestamp", "id")
        .prefetch_related("line_items")
    )
    if start:
        entries = entries.filter(timestamp__gte=start)
    if end:
        entries = entries.filter(timestamp__lte=end)

    entries = list(entries)

    sale_rows = [
        {
            "date": _display_date(e.timestamp),
            "invoice_no": e.id,
            "balance_after": _money(e.balance_after),
            "line_items": [
                {
                    "item_name": li.item_name,
                    "quantity": _quantity(li.quantity),
                    "rate": _money(li.rate),
                    "amount": _money(li.amount),
                }
                for li in e.line_items.all()
            ],
            "invoice_total": _money(e.amount),
        }
        for e in entries
        if e.type == "sale"
    ]

    payment_rows = [
        {
            "date": _display_date(e.timestamp),
            "amount": _money(e.amount),
            "note": e.note or "",
        }
        for e in entries
        if e.type == "payment"
    ]

    total_sales = sum((e.amount for e in entries if e.type == "sale"), Decimal(0))
    total_received = sum((e.amount for e in entries if e.type == "payment"), Decimal(0))
    # Balance the customer carried into this statement's period, worked
    # backwards from the first entry rather than queried separately — a
    # second, unfiltered query could disagree with the rows actually shown.
    if entries:
        first = entries[0]
        opening_balance = Decimal(first.balance_after) - Decimal(first.amount) * (
            1 if first.type == "sale" else -1
        )
    else:
        opening_balance = Decimal(customer.current_balance) - total_sales + total_received

    return {
        "business_name": business.business_name,
        "business_address": business.address,
        "business_phone": business.phone,
        "business_email": business.email,
        "currency_code": business.currency_code,
        "customer_name": customer.name,
        "customer_phone": customer.phone,
        "statement_date": _display_date(timezone.localtime(timezone.now())),
        "date_from": date_from or "—",
        "date_to": date_to or "—",
        "opening_balance": _money(opening_balance),
        "sale_rows": sale_rows,
        "payment_rows": payment_rows,
        "total_sales": _money(total_sales),
        "total_received": _money(total_received),
        "current_balance": _money(customer.current_balance),
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
        # DocumentError.message ends up stored on DocumentDelivery.error_message
        # and, from there, shown to the business owner (chat status answers,
        # the Send Document sheet) — it must never carry hostnames/ports/
        # connection internals from the raw exception. Logged server-side only.
        logger.warning("FastAPI document render service unreachable: %s", exc)
        raise DocumentError("RENDER_UNAVAILABLE", "Document service is not reachable.")

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


def resolve_latest_entry_for_customer(business, customer, entry_type):
    """The most recent matching entry for a customer, or None.

    Needed so "send Ali's last invoice" resolves without the owner (or the
    model) naming a specific entry id — `build_payload_for` otherwise
    requires an explicit `target_id`.
    """
    return (
        ActivityEntry.objects.filter(business=business, customer=customer, type=entry_type)
        .order_by("-timestamp", "-id")
        .first()
    )


def resolve_document_request(business, *, doc_type, customer=None, date_from=None, date_to=None, entry=None):
    """Server-side re-validation shared by `ConfirmDraftDocumentView` (the
    owner taps to confirm) and `apps.agent.capabilities` (the auto-execute
    path) — one validation path, not two. Never trusts a date or an id as
    given; re-resolves everything against real data, the same way
    `apps.chat.views.build_sale_from_draft` does for a bill draft.

    Raises DocumentError on anything that can't be resolved — every caller
    turns that into a clarifying question or an honest explanation, never a
    guess.
    """
    if doc_type not in ALL_DOC_TYPES:
        raise DocumentError("UNSUPPORTED_DOC_TYPE", f"Unsupported doc_type: {doc_type}")

    try:
        resolved_from = resolve_business_date(date_from) if date_from else None
        resolved_to = resolve_business_date(date_to) if date_to else None
    except BusinessDateError as exc:
        raise DocumentError("INVALID_DATE", str(exc))

    if resolved_from and resolved_to and resolved_from > resolved_to:
        raise DocumentError("INVALID_RANGE", "The start date is after the end date.")

    if doc_type == "statement" and customer is None:
        raise DocumentError("CUSTOMER_NOT_MATCHED", "Customer not found.")

    if doc_type in ("invoice", "receipt") and entry is None:
        raise DocumentError("NOT_FOUND", "That record could not be found.")

    return doc_type, customer, resolved_from, resolved_to, entry


def queue_document_send(business, *, doc_type, customer=None, entry=None, date_from=None, date_to=None,
                         requested_format=None):
    """Generalizes `queue_invoice_send` to every document type — the one safe
    pipeline for invoice/receipt (entry-based) and statement/report
    (date-range based) sends. Same non-raising, structured-outcome contract:
    every "can't send" case here is a *reason* the caller surfaces plainly,
    never an exception.
    """
    from apps.billing.exceptions import FeatureNotOnPlan, UsageCapExceeded
    from apps.billing.services import enforce_feature_gate
    from apps.jobs.dispatch import enqueue

    from .models import DocumentDelivery

    if not business.gateway_session_id:
        return {"sent": False, "reason": "NOT_CONNECTED"}
    to_phone = customer.phone if customer else None
    if not to_phone:
        return {"sent": False, "reason": "NO_PHONE"}

    try:
        enforce_feature_gate(business, "whatsapp_send")
    except FeatureNotOnPlan:
        return {"sent": False, "reason": "NOT_ON_PLAN"}
    except UsageCapExceeded:
        return {"sent": False, "reason": "QUOTA_EXCEEDED"}

    output_format = requested_format or DEFAULT_FORMAT[doc_type]
    if output_format not in SUPPORTED_FORMATS[doc_type]:
        output_format = DEFAULT_FORMAT[doc_type]

    delivery = DocumentDelivery.objects.create(
        business=business,
        customer=customer,
        doc_type=doc_type,
        requested_format=output_format,
        to_phone=to_phone,
        related_entry=entry,
        parameters={
            "target_id": entry.id if entry else None,
            "customer_id": customer.id if customer else None,
            "date_from": date_from.isoformat() if date_from else None,
            "date_to": date_to.isoformat() if date_to else None,
        },
    )
    job = enqueue(business=business, type="document_send", payload={"delivery_id": delivery.id})
    delivery.job_task = job
    delivery.save(update_fields=["job_task"])
    return {"sent": True, "delivery_id": delivery.id, "job_id": job.id}


def queue_invoice_send(business, entry, customer):
    """Queues an invoice for `entry` to `customer`'s WhatsApp. Thin wrapper
    over `queue_document_send` kept for `ConfirmDraftBillView`'s existing
    call site — every "can't send" case is a *reason*, not an error: the
    caller has already recorded the sale on the ledger, and a bill that is
    saved but not delivered must never look like a bill that failed to save.
    """
    return queue_document_send(business, doc_type="invoice", customer=customer, entry=entry)
