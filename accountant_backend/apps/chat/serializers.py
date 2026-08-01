from rest_framework import serializers

from .models import ChatMessage, Conversation


class DraftBillSerializer(serializers.Serializer):
    customer_id = serializers.CharField(allow_null=True, required=False)
    customer_name_guess = serializers.CharField(allow_null=True, required=False, allow_blank=True)
    # The matched customer's real name, filled server-side (never by the model)
    # so the card and Edit Draft screen have something to show once
    # customer_name_guess has been cleared by a successful match.
    customer_name = serializers.CharField(allow_null=True, required=False, allow_blank=True)
    previous_balance = serializers.FloatField()
    total_amount = serializers.FloatField()
    payment_received = serializers.FloatField()
    # Optional line items, so a draft built from a photographed bill can carry
    # what was actually bought through to the ledger and the invoice. Shape is
    # re-validated in apps.chat.views.build_sale_from_draft before anything is
    # written — these numbers come from a model, not a form.
    items = serializers.ListField(child=serializers.DictField(), required=False, allow_null=True)
    # Set by the model ONLY when the owner explicitly asked for the bill to be
    # recorded ("save kar do", "record it"). It skips the Confirm tap, so it is
    # the one field here that can move money without a second human action —
    # every validation in build_sale_from_draft still runs regardless.
    save_now = serializers.BooleanField(required=False, default=False)
    # The accounting date the owner asked for ("yesterday", "25 July"). Kept as
    # a free-form string rather than a DateField: the model may answer with a
    # relative phrase, and apps.sales.business_date resolves it server-side
    # against the business calendar rather than trusting the model's arithmetic.
    date = serializers.CharField(required=False, allow_null=True, allow_blank=True)


class DocumentReadySerializer(serializers.Serializer):
    document_type = serializers.ChoiceField(choices=["invoice", "statement", "receipt", "report"])
    document_url = serializers.CharField()


class DraftActionChangesSerializer(serializers.Serializer):
    """Only the fields actually being changed are present — a partial
    update, same as `edit_sale`/`edit_payment`'s own optional args."""

    amount = serializers.FloatField(required=False)
    payment_method = serializers.ChoiceField(
        choices=["cash", "bank", "jazzcash", "easypaisa"], required=False, allow_null=True
    )
    date = serializers.DateField(required=False)
    items = serializers.ListField(child=serializers.DictField(), required=False)


class DraftActionSerializer(serializers.Serializer):
    """An AI-proposed edit or transfer of an *existing* entry — see
    business_logic.md's "AI can modify records, not just create them" case.
    `entry_id`/`customer_id` must reference a real entry already visible in
    this business's data; the confirm view re-validates that server-side,
    never trusts the AI's numbers blindly."""

    action_type = serializers.ChoiceField(choices=["edit_entry", "transfer_entry"])
    entry_id = serializers.IntegerField()
    customer_id = serializers.IntegerField()
    target_customer_id = serializers.IntegerField(required=False, allow_null=True)
    changes = DraftActionChangesSerializer(required=False)
    summary = serializers.CharField()

    def validate(self, attrs):
        if attrs["action_type"] == "transfer_entry" and not attrs.get("target_customer_id"):
            raise serializers.ValidationError("target_customer_id is required for transfer_entry.")
        if attrs["action_type"] == "edit_entry" and not attrs.get("changes"):
            raise serializers.ValidationError("changes is required for edit_entry.")
        return attrs


class DraftDocumentSerializer(serializers.Serializer):
    """An AI-proposed statement or report over an explicit date range.

    Distinct from `document_ready`, which requires a URL the model was handed
    and must never invent. This is a *request* to generate one — the owner
    confirms it, and the server builds the document from the ledger.

    Dates stay free-form strings so `apps.sales.business_date` resolves them
    server-side, the same as `draft_bill.date`.
    """

    doc_type = serializers.ChoiceField(choices=["statement", "report"])
    customer_id = serializers.IntegerField(required=False, allow_null=True)
    date_from = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    date_to = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    summary = serializers.CharField()

    def validate(self, attrs):
        if attrs["doc_type"] == "statement" and not attrs.get("customer_id"):
            raise serializers.ValidationError("customer_id is required for a statement.")
        return attrs


class AiReplySerializer(serializers.Serializer):
    text = serializers.CharField()
    speech_text = serializers.CharField(allow_null=True, required=False, allow_blank=True)
    draft_bill = DraftBillSerializer(allow_null=True, required=False)
    document_ready = DocumentReadySerializer(allow_null=True, required=False)
    draft_action = DraftActionSerializer(allow_null=True, required=False)
    draft_document = DraftDocumentSerializer(allow_null=True, required=False)

    def validate(self, attrs):
        present = [
            k for k in ("draft_bill", "document_ready", "draft_action", "draft_document")
            if attrs.get(k)
        ]
        if len(present) > 1:
            raise serializers.ValidationError(
                "draft_bill, document_ready, draft_action and draft_document are mutually exclusive."
            )
        return attrs


class ChatMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChatMessage
        fields = [
            "id",
            "sender",
            "text",
            "speech_text",
            "image_url",
            "draft_bill",
            "draft_confirmed",
            "document_ready",
            "draft_action",
            "draft_document",
            "is_error_fallback",
            "timestamp",
            "updated_at",
        ]


class ConversationSerializer(serializers.ModelSerializer):
    """Conversation summary for the history list."""

    message_count = serializers.IntegerField(read_only=True)
    last_message_at = serializers.DateTimeField(read_only=True, allow_null=True)
    preview = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = ["id", "created_at", "updated_at", "message_count", "last_message_at", "preview"]

    def get_preview(self, conversation):
        """First line of the most recent message, for the list row."""
        last = conversation.messages.order_by("-timestamp", "-id").first()
        if last is None or not last.text:
            return ""
        return last.text.strip().splitlines()[0][:120]
