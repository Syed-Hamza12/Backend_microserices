from decimal import Decimal

from rest_framework import serializers

from .business_date import BusinessDateError, resolve as resolve_business_date
from .models import ActivityEntry, SaleLineItem


def _validated_entry_date(value):
    """Applies the same business-date rules to manual entries as to AI ones.

    Past, today and future are all valid — a future date is a planned bill, and
    `recalculate_balances` keeps it out of what is owed today. What this rejects
    is a date so far out in either direction that it is almost certainly a
    mistyped year, which would either rewrite the ledger behind it or sit
    un-matured and invisible for years.

    The rule can't live only on the AI path: the manual form is where most
    entries come from.
    """
    if value is None:
        return None
    try:
        resolve_business_date(value)
    except BusinessDateError as exc:
        raise serializers.ValidationError(str(exc))
    return value

# Money and quantity bounds applied to every write path.
#
# Nothing previously stopped a negative quantity, rate, or amount from being
# saved. A negative sale reduces what a customer owes and a negative payment
# increases it, so an unvalidated request could rewrite a balance in either
# direction — the ledger's core invariant ("entries only move the balance the
# way their type says") had no enforcement behind it at all.
MIN_MONEY = Decimal("0.01")
MAX_MONEY = Decimal("9999999999.99")
MIN_QUANTITY = Decimal("0.01")
MAX_QUANTITY = Decimal("9999999999.99")


class SaleLineItemSerializer(serializers.ModelSerializer):
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)

    class Meta:
        model = SaleLineItem
        fields = ["id", "item_name", "quantity", "rate", "amount"]


class SaleLineItemInputSerializer(serializers.Serializer):
    item_name = serializers.CharField(max_length=255)
    quantity = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=MIN_QUANTITY, max_value=MAX_QUANTITY
    )
    rate = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=MIN_MONEY, max_value=MAX_MONEY)


class ActivityEntrySerializer(serializers.ModelSerializer):
    line_items = SaleLineItemSerializer(many=True, read_only=True)
    customer_id = serializers.IntegerField(source="customer.id", read_only=True)
    customer_name = serializers.CharField(source="customer.name", read_only=True)
    # True once a document for this entry has been accepted by WhatsApp. Editing
    # such an entry leaves the customer holding a document that no longer
    # matches the ledger, so the app warns before saving and offers to resend.
    document_sent = serializers.SerializerMethodField()

    def get_document_sent(self, entry):
        return entry.document_deliveries.filter(status="accepted").exists()

    class Meta:
        model = ActivityEntry
        fields = [
            "id",
            "customer_id",
            "customer_name",
            "type",
            "amount",
            "balance_after",
            "timestamp",
            "sale_group_id",
            "payment_method",
            "note",
            "created_by",
            "line_items",
            "created_at",
            "updated_at",
            "document_sent",
        ]


class RecordSaleSerializer(serializers.Serializer):
    customer_id = serializers.IntegerField()
    items = SaleLineItemInputSerializer(many=True)
    amount_received = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, default=0, min_value=Decimal("0"), max_value=MAX_MONEY
    )
    payment_method = serializers.ChoiceField(
        choices=ActivityEntry.PAYMENT_METHOD_CHOICES, required=False, allow_null=True, default=None
    )
    date = serializers.DateTimeField(required=False, allow_null=True, default=None)

    def validate_date(self, value):
        return _validated_entry_date(value)

    def validate(self, attrs):
        if attrs.get("amount_received") and not attrs.get("payment_method"):
            raise serializers.ValidationError("payment_method is required when amount_received > 0.")
        items = attrs.get("items")
        if not items:
            raise serializers.ValidationError("At least one item is required.")

        # Paying more than the sale is worth is a data-entry mistake, not a
        # transaction: it silently turns into a credit on the customer's
        # balance that nobody asked for.
        total = sum(item["quantity"] * item["rate"] for item in items)
        if attrs.get("amount_received", 0) > total:
            raise serializers.ValidationError("amount_received cannot be greater than the sale total.")
        return attrs


class ConcurrencyGuardMixin(serializers.Serializer):
    """Optimistic locking for edits, using the row's own `updated_at`.

    The client sends the `updated_at` it last saw. If the stored value has moved
    on, someone else changed the entry first and this edit would silently
    overwrite theirs — so it is refused and the owner is told to reload.

    Chosen over row locking deliberately: `select_for_update` is a no-op on
    SQLite, so a lock-based approach would look correct and do nothing on the
    database this actually runs on. A timestamp comparison works identically on
    both engines. The field is optional, so existing clients keep working —
    they simply don't get the protection until they send it.
    """

    expected_updated_at = serializers.DateTimeField(required=False, allow_null=True)


class EditSaleSerializer(ConcurrencyGuardMixin):
    items = SaleLineItemInputSerializer(many=True, required=False)
    date = serializers.DateTimeField(required=False, allow_null=True)

    def validate_date(self, value):
        return _validated_entry_date(value)

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError("A sale must keep at least one item.")
        return value


class RecordPaymentSerializer(serializers.Serializer):
    customer_id = serializers.IntegerField()
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=MIN_MONEY, max_value=MAX_MONEY)
    payment_method = serializers.ChoiceField(choices=ActivityEntry.PAYMENT_METHOD_CHOICES)
    date = serializers.DateTimeField(required=False, allow_null=True, default=None)
    note = serializers.CharField(required=False, allow_blank=True, default="", max_length=2000)

    def validate_date(self, value):
        return _validated_entry_date(value)


class EditPaymentSerializer(ConcurrencyGuardMixin):
    amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, min_value=MIN_MONEY, max_value=MAX_MONEY
    )
    payment_method = serializers.ChoiceField(choices=ActivityEntry.PAYMENT_METHOD_CHOICES, required=False)
    date = serializers.DateTimeField(required=False, allow_null=True)
    note = serializers.CharField(required=False, allow_blank=True, max_length=2000)

    def validate_date(self, value):
        return _validated_entry_date(value)
