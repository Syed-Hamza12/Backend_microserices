from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import Business
from apps.accounts.pagination import paginated_response
from apps.customers.models import Customer

from . import services
from .models import ActivityEntry, PendingUndo
from .serializers import (
    ActivityEntrySerializer,
    EditPaymentSerializer,
    EditSaleSerializer,
    RecordPaymentSerializer,
    RecordSaleSerializer,
)


def _business_or_error(request):
    try:
        return request.user.business, None
    except Business.DoesNotExist:
        return None, Response(
            {"success": False, "error": {"code": "NO_BUSINESS", "message": "No business created yet."}},
            status=status.HTTP_404_NOT_FOUND,
        )


def _not_found(code="NOT_FOUND", message="Not found."):
    return Response({"success": False, "error": {"code": code, "message": message}}, status=status.HTTP_404_NOT_FOUND)


def _stale_edit_response():
    """409 for an edit built on a version of the entry that has since changed.

    Returned instead of applying the write, so a second device can't silently
    overwrite a correction made on the first. The client reloads and decides.
    """
    return Response(
        {
            "success": False,
            "error": {
                "code": "ENTRY_MODIFIED",
                "message": "This record was changed on another device. Reload it and try again.",
            },
        },
        status=status.HTTP_409_CONFLICT,
    )


# Tolerance for comparing the client's copy of `updated_at` with the stored one.
# Not a "close enough" fudge: it absorbs formatting drift from a JSON round-trip
# while staying far below the interval between two genuine edits. Rounding to
# whole seconds (the obvious shortcut) would defeat the guard entirely — two
# devices saving within the same second is precisely the collision being caught.
VERSION_TOLERANCE_SECONDS = 0.001


def _is_stale(entry, expected_updated_at):
    """True when the caller's copy is behind the stored row."""
    if expected_updated_at is None:
        return False
    drift = abs((entry.updated_at - expected_updated_at).total_seconds())
    return drift > VERSION_TOLERANCE_SECONDS


class CustomerHistoryView(APIView):
    def get(self, request, customer_id):
        business, error = _business_or_error(request)
        if error:
            return error
        try:
            customer = Customer.objects.get(business=business, pk=customer_id)
        except Customer.DoesNotExist:
            return _not_found(message="Customer not found.")
        entries = (
            ActivityEntry.objects.filter(business=business, customer=customer)
            .select_related("customer")
            .prefetch_related("line_items")
            .order_by("timestamp", "id")
        )
        return Response(paginated_response(ActivityEntrySerializer, entries, request))


class EntriesInRangeView(APIView):
    """Every entry across ALL customers in a date range — backs the chat
    "View" button on a `report_view` card (apps.chat.services._attach_report_view)
    for period questions like "pichle hafte ki detail batao" or "10 se 20
    tareek ka hisaab", where a business owner wants to see everyone who
    bought something in a window, not one customer's ledger. `date_from`/
    `date_to` are required and re-validated the same way every other date in
    this codebase is — never trusted as a raw string straight into a query.
    """

    def get(self, request):
        business, error = _business_or_error(request)
        if error:
            return error

        from apps.sales.business_date import BusinessDateError, resolve as resolve_business_date

        raw_from = request.query_params.get("date_from")
        raw_to = request.query_params.get("date_to")
        if not raw_from or not raw_to:
            return Response(
                {"success": False, "error": {"code": "INVALID_RANGE", "message": "date_from and date_to are required."}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            date_from = resolve_business_date(raw_from)
            date_to = resolve_business_date(raw_to)
        except BusinessDateError as exc:
            return Response(
                {"success": False, "error": {"code": "INVALID_RANGE", "message": str(exc)}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if date_from is None or date_to is None or date_from > date_to:
            return Response(
                {"success": False, "error": {"code": "INVALID_RANGE", "message": "date_from must be on or before date_to."}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        entries = (
            ActivityEntry.objects.filter(business=business, timestamp__date__range=(date_from, date_to))
            .select_related("customer")
            .prefetch_related("line_items")
            .order_by("-timestamp", "-id")
        )
        return Response(paginated_response(ActivityEntrySerializer, entries, request))


class DashboardSummaryView(APIView):
    """Business-at-a-glance figures for the Home tab.

    The app deliberately calculates none of these itself
    (docx/BUSINESS_LOGIC.md); until this existed the Dashboard had no endpoint
    to call at all and shipped showing hardcoded sample numbers to real owners.

    "Today" is the business's own calendar day via `timezone.localdate()`, not
    UTC — a shop in Karachi closing at 11pm must not see its evening sales land
    on tomorrow's total.
    """

    #: Recent-activity rows returned. Matches what the Home tab shows without
    #: pulling the whole ledger into a screen that only previews it.
    RECENT_LIMIT = 20

    def get(self, request):
        business, error = _business_or_error(request)
        if error:
            return error

        today = timezone.localdate()
        todays_entries = ActivityEntry.objects.filter(business=business, timestamp__date=today)

        todays_sales = todays_entries.filter(type="sale").aggregate(total=Sum("amount"))["total"] or Decimal("0")
        todays_payments = todays_entries.filter(type="payment").aggregate(total=Sum("amount"))["total"] or Decimal("0")

        # Only positive balances are receivable. Summing raw balances would let
        # one customer's overpayment (a credit, stored negative) quietly cancel
        # out another customer's debt and understate what the shop is owed.
        total_receivable = Customer.objects.filter(
            business=business, current_balance__gt=0
        ).aggregate(total=Sum("current_balance"))["total"] or Decimal("0")

        recent = (
            ActivityEntry.objects.filter(business=business)
            .select_related("customer")
            .prefetch_related("line_items")
            .order_by("-timestamp", "-id")[: self.RECENT_LIMIT]
        )

        return Response(
            {
                "success": True,
                "data": {
                    "todays_sales": todays_sales,
                    "todays_payments_received": todays_payments,
                    "total_receivable": total_receivable,
                    "recent_activity": ActivityEntrySerializer(recent, many=True).data,
                },
            }
        )


class RecordSaleView(APIView):
    def post(self, request):
        business, error = _business_or_error(request)
        if error:
            return error
        serializer = RecordSaleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            customer = Customer.objects.get(business=business, pk=data["customer_id"])
        except Customer.DoesNotExist:
            return _not_found(message="Customer not found.")

        sale_entry, payment_entry = services.record_sale(
            business=business,
            customer=customer,
            items=data["items"],
            amount_received=data["amount_received"],
            payment_method=data["payment_method"],
            date=data["date"],
            created_by="manual",
        )
        return Response(
            {
                "success": True,
                "data": {
                    "sale": ActivityEntrySerializer(sale_entry).data,
                    "payment": ActivityEntrySerializer(payment_entry).data if payment_entry else None,
                },
            },
            status=status.HTTP_201_CREATED,
        )


class EditSaleView(APIView):
    def patch(self, request, entry_id):
        business, error = _business_or_error(request)
        if error:
            return error
        try:
            entry = ActivityEntry.objects.get(business=business, pk=entry_id, type="sale")
        except ActivityEntry.DoesNotExist:
            return _not_found(message="Sale not found.")
        serializer = EditSaleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if _is_stale(entry, data.get("expected_updated_at")):
            return _stale_edit_response()
        entry = services.edit_sale(entry=entry, items=data.get("items"), date=data.get("date"))
        return Response({"success": True, "data": ActivityEntrySerializer(entry).data})


class DeleteSaleLineItemView(APIView):
    def delete(self, request, entry_id, index):
        business, error = _business_or_error(request)
        if error:
            return error
        try:
            entry = ActivityEntry.objects.get(business=business, pk=entry_id, type="sale")
        except ActivityEntry.DoesNotExist:
            return _not_found(message="Sale not found.")
        try:
            entry = services.delete_sale_line_item(entry=entry, index=index)
        except IndexError:
            return _not_found(message="Line item index out of range.")
        return Response({"success": True, "data": ActivityEntrySerializer(entry).data})


class RecordPaymentView(APIView):
    def post(self, request):
        business, error = _business_or_error(request)
        if error:
            return error
        serializer = RecordPaymentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            customer = Customer.objects.get(business=business, pk=data["customer_id"])
        except Customer.DoesNotExist:
            return _not_found(message="Customer not found.")

        entry = services.record_payment(
            business=business,
            customer=customer,
            amount=data["amount"],
            method=data["payment_method"],
            date=data["date"],
            note=data["note"],
            created_by="manual",
        )
        return Response({"success": True, "data": ActivityEntrySerializer(entry).data}, status=status.HTTP_201_CREATED)


class EditPaymentView(APIView):
    def patch(self, request, entry_id):
        business, error = _business_or_error(request)
        if error:
            return error
        try:
            entry = ActivityEntry.objects.get(business=business, pk=entry_id, type="payment")
        except ActivityEntry.DoesNotExist:
            return _not_found(message="Payment not found.")
        serializer = EditPaymentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if _is_stale(entry, data.get("expected_updated_at")):
            return _stale_edit_response()
        entry = services.edit_payment(
            entry=entry,
            amount=data.get("amount"),
            method=data.get("payment_method"),
            date=data.get("date"),
            note=data.get("note"),
        )
        return Response({"success": True, "data": ActivityEntrySerializer(entry).data})


class DeleteEntryView(APIView):
    def delete(self, request, entry_id):
        business, error = _business_or_error(request)
        if error:
            return error
        try:
            entry = ActivityEntry.objects.get(business=business, pk=entry_id)
        except ActivityEntry.DoesNotExist:
            return _not_found(message="Entry not found.")
        services.delete_entry(entry=entry)
        return Response({"success": True, "data": {}})


class UndoActionView(APIView):
    """Reverts one AI-confirmed edit/transfer (`apps.chat.views.ConfirmDraftActionView`)
    within its 5-minute window — see `services.undo_pending_action`. Not
    usable for anything else (manual edits, sale confirmations, etc.) —
    this is deliberately scoped to the AI-action safety net only."""

    def post(self, request, pending_undo_id):
        business, error = _business_or_error(request)
        if error:
            return error
        try:
            pending_undo = PendingUndo.objects.get(business=business, pk=pending_undo_id)
        except PendingUndo.DoesNotExist:
            return _not_found(message="Nothing to undo.")

        try:
            services.undo_pending_action(pending_undo=pending_undo)
        except ValueError as exc:
            return Response(
                {"success": False, "error": {"code": "UNDO_EXPIRED", "message": str(exc)}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({"success": True, "data": {}})
