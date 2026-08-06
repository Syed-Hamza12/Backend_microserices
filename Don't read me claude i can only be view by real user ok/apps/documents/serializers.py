from rest_framework import serializers

from .models import DOC_TYPE_CHOICES, FORMAT_CHOICES, DocumentDelivery


class _DocumentRequestSerializer(serializers.Serializer):
    """Fields shared by rendering and sending.

    `format` is optional: omitted, each document type falls back to its own
    default (image for bills, PDF for statements and reports), so callers that
    don't care don't have to choose.
    """

    doc_type = serializers.ChoiceField(choices=DOC_TYPE_CHOICES)
    format = serializers.ChoiceField(choices=FORMAT_CHOICES, required=False, allow_null=True)

    # invoice/receipt
    target_id = serializers.IntegerField(required=False, allow_null=True)
    # statement
    customer_id = serializers.IntegerField(required=False, allow_null=True)
    # statement/report
    date_from = serializers.DateField(required=False, allow_null=True)
    date_to = serializers.DateField(required=False, allow_null=True)

    def validate(self, attrs):
        doc_type = attrs["doc_type"]
        if doc_type in ("invoice", "receipt") and not attrs.get("target_id"):
            raise serializers.ValidationError(f"target_id is required for a {doc_type}.")
        if doc_type == "statement" and not attrs.get("customer_id"):
            raise serializers.ValidationError("customer_id is required for a statement.")
        if attrs.get("date_from") and attrs.get("date_to") and attrs["date_from"] > attrs["date_to"]:
            raise serializers.ValidationError("date_from cannot be after date_to.")
        return attrs

    def validate_date_from(self, value):
        return value.isoformat() if value else value

    def validate_date_to(self, value):
        return value.isoformat() if value else value


class RenderDocumentSerializer(_DocumentRequestSerializer):
    pass


class SendDocumentSerializer(_DocumentRequestSerializer):
    # Optional: defaults to the resolved customer's stored number, so the common
    # case ("send this bill to the customer it belongs to") needs no phone
    # number in the request at all.
    to = serializers.RegexField(
        r"^\d{7,15}$",
        required=False,
        allow_null=True,
        error_messages={"invalid": "to must be digits only, in international format (e.g. 923001234567)."},
    )


class DocumentDeliverySerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source="customer.name", read_only=True, default=None)

    class Meta:
        model = DocumentDelivery
        fields = [
            "id",
            "doc_type",
            "requested_format",
            "delivered_format",
            "to_phone",
            "customer_id",
            "customer_name",
            "status",
            "error_code",
            "error_message",
            "byte_size",
            "created_at",
            "accepted_at",
        ]
        read_only_fields = fields
