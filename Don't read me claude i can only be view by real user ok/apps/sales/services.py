import uuid
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from apps.customers.models import Customer
from apps.notifications.services import create_notification

from .business_date import BusinessDateError, business_today, resolve as resolve_business_date
from .models import ActivityEntry, EntryChangeLog, PendingUndo, SaleLineItem

# How long an AI-confirmed edit/transfer stays revertible via PendingUndo —
# short by design: this is a "I confirmed too fast" safety net, not a
# general-purpose undo-history feature.
UNDO_WINDOW = timedelta(minutes=5)

# Bounds for anything an AI proposes writing into the ledger. Matches the
# DecimalField(max_digits=12, decimal_places=2) columns these end up in.
MAX_ENTRY_AMOUNT = Decimal("9999999999.99")
MAX_ENTRY_ITEMS = 50


def recalculate_balances(customer):
    """Recomputes balance_after for every entry of this customer, in
    (timestamp, id) order, from the customer's opening_balance forward.

    Two balances come out of one walk:

    * `current_balance` — what is owed **today**. Entries dated in the future are
      walked (so their own `balance_after` is right) but do not move this figure.
      A sale dated next month is not money owed now, and including it would put
      an inflated number on the dashboard, in every statement, and in the
      `previous_balance` of the next AI draft — the owner would chase a customer
      for a bill that isn't due.
    * `projected_balance` — where the balance lands once everything scheduled has
      matured. Equal to `current_balance` when nothing is future-dated.

    Always correct regardless of which entry was just edited, deleted or
    inserted: entries upstream of the change keep the `balance_after` they
    already had, because nothing before them moved.

    Takes a row lock on the customer first. Without it, two entries saved for
    the same customer at the same moment both read the ledger, both walk it,
    and the second write silently overwrites the first — the recorded
    current_balance then reflects only one of the two transactions. That is a
    money-losing race, not a slow-query concern, so it holds even though it
    serialises writes per customer.
    """
    caller_instance = customer
    customer = Customer.objects.select_for_update().get(pk=customer.pk)
    today = business_today()

    running = customer.opening_balance
    as_of_today = running
    entries = list(ActivityEntry.objects.filter(customer=customer).order_by("timestamp", "id"))
    for entry in entries:
        if entry.type == "sale":
            running += entry.amount
        else:
            running -= entry.amount
        if entry.balance_after != running:
            entry.balance_after = running
            entry.save(update_fields=["balance_after"])
        # Entries are in date order, so this tracks the running total up to and
        # including today and stops advancing once the walk reaches the future.
        if timezone.localtime(entry.timestamp).date() <= today:
            as_of_today = running

    customer.current_balance = as_of_today
    customer.projected_balance = running
    customer.save(update_fields=["current_balance", "projected_balance"])
    # Keep the caller's own instance in step with what was just written, so code
    # that reads these after this call (draft bills, serialised responses)
    # doesn't hand back a stale figure.
    caller_instance.current_balance = as_of_today
    caller_instance.projected_balance = running


@transaction.atomic
def record_sale(*, business, customer, items, amount_received=0, payment_method=None, date=None, created_by="manual"):
    sale_time = date or timezone.now()
    sale_total = sum((item["quantity"] * item["rate"] for item in items))
    group_id = uuid.uuid4() if amount_received else None

    sale_entry = ActivityEntry.objects.create(
        business=business,
        customer=customer,
        type="sale",
        amount=sale_total,
        balance_after=0,
        timestamp=sale_time,
        sale_group_id=group_id,
        created_by=created_by,
    )
    for item in items:
        SaleLineItem.objects.create(
            entry=sale_entry,
            item_name=item["item_name"],
            quantity=item["quantity"],
            rate=item["rate"],
        )

    payment_entry = None
    if amount_received:
        payment_entry = ActivityEntry.objects.create(
            business=business,
            customer=customer,
            type="payment",
            amount=amount_received,
            balance_after=0,
            timestamp=sale_time + timedelta(milliseconds=1),
            sale_group_id=group_id,
            payment_method=payment_method,
            created_by=created_by,
        )

    recalculate_balances(customer)
    sale_entry.refresh_from_db()
    if payment_entry:
        payment_entry.refresh_from_db()
        create_notification(
            business,
            "payment_received",
            payload={"customer_id": customer.id, "customer_name": customer.name, "amount": str(amount_received)},
        )
    return sale_entry, payment_entry


@transaction.atomic
def edit_sale(*, entry, items=None, date=None):
    if items is not None:
        entry.line_items.all().delete()
        sale_total = 0
        for item in items:
            SaleLineItem.objects.create(
                entry=entry,
                item_name=item["item_name"],
                quantity=item["quantity"],
                rate=item["rate"],
            )
            sale_total += item["quantity"] * item["rate"]
        entry.amount = sale_total
    if date is not None:
        entry.timestamp = date
    entry.save(update_fields=["amount", "timestamp", "updated_at"])
    recalculate_balances(entry.customer)
    entry.refresh_from_db()
    return entry


@transaction.atomic
def delete_sale_line_item(*, entry, index):
    line_items = list(entry.line_items.all().order_by("id"))
    if index < 0 or index >= len(line_items):
        raise IndexError("line item index out of range")
    line_items[index].delete()
    remaining = entry.line_items.all()
    entry.amount = sum(li.quantity * li.rate for li in remaining)
    entry.save(update_fields=["amount", "updated_at"])
    recalculate_balances(entry.customer)
    entry.refresh_from_db()
    return entry


@transaction.atomic
def record_payment(*, business, customer, amount, method, date=None, note="", created_by="manual"):
    entry = ActivityEntry.objects.create(
        business=business,
        customer=customer,
        type="payment",
        amount=amount,
        balance_after=0,
        timestamp=date or timezone.now(),
        payment_method=method,
        note=note,
        created_by=created_by,
    )
    recalculate_balances(customer)
    entry.refresh_from_db()
    create_notification(
        business,
        "payment_received",
        payload={"customer_id": customer.id, "customer_name": customer.name, "amount": str(amount)},
    )
    return entry


@transaction.atomic
def edit_payment(*, entry, amount=None, method=None, date=None, note=None):
    if amount is not None:
        entry.amount = amount
    if method is not None:
        entry.payment_method = method
    if date is not None:
        entry.timestamp = date
    if note is not None:
        entry.note = note
    entry.save()
    recalculate_balances(entry.customer)
    entry.refresh_from_db()
    return entry


@transaction.atomic
def delete_entry(*, entry):
    """Deletes an entry, and its same-transaction sibling when there is one.

    A sale recorded with a payment attached is stored as two rows sharing a
    `sale_group_id`. Deleting only one of them left the other stranded: delete
    the sale and the customer keeps a payment against a transaction that no
    longer exists, which shows up as an unexplained credit on their balance.
    `transfer_entry` already treated the pair as a unit; deletion has to as
    well.
    """
    customer = entry.customer
    if entry.sale_group_id:
        ActivityEntry.objects.filter(business=entry.business, sale_group_id=entry.sale_group_id).delete()
    else:
        entry.delete()
    recalculate_balances(customer)


def _snapshot_entry(entry):
    """Everything needed to fully restore this entry later — used both for
    the audit log (old_values/new_values) and for PendingUndo's revert
    snapshot. Decimal/UUID/datetime fields are stringified since this goes
    straight into a JSONField."""
    return {
        "customer_id": entry.customer_id,
        "type": entry.type,
        "amount": str(entry.amount),
        "timestamp": entry.timestamp.isoformat(),
        "payment_method": entry.payment_method,
        "line_items": [
            {"item_name": li.item_name, "quantity": str(li.quantity), "rate": str(li.rate)}
            for li in entry.line_items.all()
        ] if entry.type == "sale" else None,
    }


def _clean_money(value, field_name):
    """Model-supplied number -> Decimal, or ValueError with a message the owner
    can act on. Goes through str() so a JSON float doesn't carry its binary
    approximation into a money column."""
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"The proposed {field_name} isn't a valid number.")
    if amount <= 0:
        raise ValueError(f"The proposed {field_name} must be greater than zero.")
    if amount > MAX_ENTRY_AMOUNT:
        raise ValueError(f"The proposed {field_name} is unrealistically large.")
    return amount


def _clean_proposed_items(raw_items):
    """Validates the line items an AI proposed for an existing sale.

    These arrive as free-form dicts (the draft serializer accepts any shape), so
    without this a hallucinated item reached `edit_sale`, blew up mid-write on a
    missing key or a string where a number belonged, and surfaced to the owner
    as a bare "Could not apply this change".
    """
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("The proposed items are empty or unreadable.")
    if len(raw_items) > MAX_ENTRY_ITEMS:
        raise ValueError(f"A sale can't have more than {MAX_ENTRY_ITEMS} items.")

    items = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise ValueError("The proposed items are in an unreadable format.")
        name = str(raw.get("item_name") or "").strip()[:255]
        if not name:
            raise ValueError("One of the proposed items has no name.")
        items.append(
            {
                "item_name": name,
                "quantity": _clean_money(raw.get("quantity"), f"quantity for {name}"),
                "rate": _clean_money(raw.get("rate"), f"rate for {name}"),
            }
        )
    return items


@transaction.atomic
def edit_entry_via_ai(*, entry, changes):
    """AI-chat-initiated edit of an *existing* entry (as opposed to
    `edit_sale`/`edit_payment`, which this wraps) — the distinction that
    matters here is every call logs an `EntryChangeLog` and creates a
    `PendingUndo`, since the owner is confirming an AI-proposed change
    rather than typing new values into a form themselves. `changes` is the
    validated `DraftActionSerializer` payload's `changes` dict.

    Raises ValueError for a proposal that isn't safe to apply; the view turns
    that into a specific message rather than a generic failure.
    """
    before = _snapshot_entry(entry)

    new_date = None
    if changes.get("date") is not None:
        # Same server-side resolution as a new draft: the model's date is a
        # request, not an instruction.
        try:
            resolved = resolve_business_date(changes["date"])
        except BusinessDateError as exc:
            raise ValueError(str(exc))
        if resolved is not None:
            # Time of day is preserved so the entry keeps its position among
            # others recorded the same day.
            new_date = datetime.combine(resolved, entry.timestamp.timetz())

    if entry.type == "sale":
        raw_items = changes.get("items")
        items = _clean_proposed_items(raw_items) if raw_items is not None else None
        if items is None and new_date is None:
            raise ValueError("The proposed change doesn't actually change anything.")
        edit_sale(entry=entry, items=items, date=new_date)
    else:
        amount = changes.get("amount")
        cleaned_amount = _clean_money(amount, "amount") if amount is not None else None
        if cleaned_amount is None and new_date is None and changes.get("payment_method") is None:
            raise ValueError("The proposed change doesn't actually change anything.")
        edit_payment(
            entry=entry,
            amount=cleaned_amount,
            method=changes.get("payment_method"),
            date=new_date,
        )

    entry.refresh_from_db()
    after = _snapshot_entry(entry)
    EntryChangeLog.objects.create(
        business=entry.business, entry_id=entry.id, action="edit", old_values=before, new_values=after
    )
    pending_undo = PendingUndo.objects.create(
        business=entry.business,
        entry_id=entry.id,
        action="edit",
        snapshot=before,
        expires_at=timezone.now() + UNDO_WINDOW,
    )
    return entry, pending_undo


def log_ai_created_sale(*, entry, source_message_id):
    """Audit row for a sale the AI drafted and the owner confirmed.

    `EntryChangeLog` previously covered only AI *edits* and *transfers*, so an
    AI-created sale left no trace of having come from the assistant beyond
    `created_by`. If someone disputes a figure the AI put on their ledger,
    "which draft produced this, and what did it say" has to be answerable.
    """
    snapshot = _snapshot_entry(entry)
    EntryChangeLog.objects.create(
        business=entry.business,
        entry_id=entry.id,
        action="create",
        old_values={},
        new_values={
            **snapshot,
            "source_message_id": source_message_id,
            # Recorded explicitly, not just inside the timestamp: "which date did
            # the AI put this on, and was it backdated or scheduled" has to be
            # answerable from the audit trail alone if the figure is ever
            # disputed.
            "business_date": timezone.localtime(entry.timestamp).date().isoformat(),
            "was_backdated": timezone.localtime(entry.timestamp).date() < business_today(),
            "was_scheduled": timezone.localtime(entry.timestamp).date() > business_today(),
        },
        source="ai_chat",
    )


@transaction.atomic
def transfer_entry(*, entry, new_customer):
    """Moves an entry (and, if it's one half of a same-transaction sale +
    payment pair, its linked sibling too — see `record_sale`'s
    `sale_group_id`) to a different customer, recalculating balances for
    BOTH the old and new customer. This is for the "owner recorded this
    against the wrong customer" case, not a general reassignment tool."""
    old_customer = entry.customer
    before = _snapshot_entry(entry)

    group_entries = [entry]
    if entry.sale_group_id:
        # Scoped to the business as well as the group id — the id alone is a
        # UUID and collisions aren't realistic, but a cross-tenant query has no
        # business existing in a money-moving path.
        group_entries = list(
            ActivityEntry.objects.filter(business=entry.business, sale_group_id=entry.sale_group_id)
        )

    entry_ids = [e.id for e in group_entries]
    for e in group_entries:
        e.customer = new_customer
        e.save(update_fields=["customer"])

    recalculate_balances(old_customer)
    recalculate_balances(new_customer)

    entry.refresh_from_db()
    after = _snapshot_entry(entry)
    EntryChangeLog.objects.create(
        business=entry.business, entry_id=entry.id, action="transfer", old_values=before, new_values=after
    )
    pending_undo = PendingUndo.objects.create(
        business=entry.business,
        entry_id=entry.id,
        action="transfer",
        snapshot={"old_customer_id": old_customer.id, "entry_ids": entry_ids},
        expires_at=timezone.now() + UNDO_WINDOW,
    )
    return entry, pending_undo


@transaction.atomic
def undo_pending_action(*, pending_undo):
    """Reverts exactly one AI-confirmed edit/transfer, within its 5-minute
    window and only once. Raises ValueError if the window has expired, it was
    already used/undone, or the entry it refers to no longer exists — the
    caller (view) turns that into a user-facing error, never a silent no-op.
    """
    # Claim the undo before doing any work. `is_valid()` followed by a save at
    # the end left a window where two taps on Undo both passed the check and
    # both reverted — applying the same restore twice. This marks it used in a
    # single conditional UPDATE, so exactly one caller can proceed.
    claimed = PendingUndo.objects.filter(
        pk=pending_undo.pk, used=False, expires_at__gt=timezone.now()
    ).update(used=True)
    if not claimed:
        raise ValueError("This action can no longer be undone.")
    pending_undo.used = True

    if pending_undo.action == "transfer":
        try:
            old_customer = Customer.objects.get(
                pk=pending_undo.snapshot["old_customer_id"], business=pending_undo.business
            )
        except Customer.DoesNotExist:
            raise ValueError("The original customer no longer exists, so this can't be undone.")
        entries = list(
            ActivityEntry.objects.filter(
                id__in=pending_undo.snapshot["entry_ids"], business=pending_undo.business
            )
        )
        if not entries:
            raise ValueError("Those records no longer exist, so this can't be undone.")
        new_customer = entries[0].customer
        for e in entries:
            e.customer = old_customer
            e.save(update_fields=["customer"])
        recalculate_balances(old_customer)
        if new_customer is not None and new_customer.pk != old_customer.pk:
            recalculate_balances(new_customer)
    else:  # "edit"
        try:
            entry = ActivityEntry.objects.get(pk=pending_undo.entry_id, business=pending_undo.business)
        except ActivityEntry.DoesNotExist:
            # Previously this raised DoesNotExist straight out of the service
            # and surfaced as a 500. It's an ordinary "too late" case.
            raise ValueError("That record no longer exists, so this can't be undone.")
        before = pending_undo.snapshot
        restore_date = datetime.fromisoformat(before["timestamp"])
        if before["type"] == "sale":
            # Decimal, not float. These values were serialised with str() from
            # DecimalFields; restoring them through float() reintroduced binary
            # rounding error into money that had been exact — an undo could
            # leave a balance a paisa off from where it started.
            items = [
                {
                    "item_name": li["item_name"],
                    "quantity": Decimal(li["quantity"]),
                    "rate": Decimal(li["rate"]),
                }
                for li in (before["line_items"] or [])
            ]
            edit_sale(entry=entry, items=items, date=restore_date)
        else:
            edit_payment(
                entry=entry,
                amount=Decimal(before["amount"]),
                method=before["payment_method"],
                date=restore_date,
            )
