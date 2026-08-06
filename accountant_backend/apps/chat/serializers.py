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
    """An AI-proposed document — a statement/report over a date range, or a
    resend of a customer's existing invoice/receipt (the server finds their
    latest one; no entry id is expected from the model — see
    `apps.agent.capabilities.find_latest_entry`).

    Distinct from `document_ready`, which requires a URL the model was handed
    and must never invent. This is a *request* to generate one. Whether it
    executes immediately or waits for an owner tap is decided by
    `apps.agent.planner`/`apps.chat.services.apply_safe_document_send` based
    on resolvability — not by anything the model sets here.

    Dates stay free-form strings so `apps.sales.business_date` resolves them
    server-side, the same as `draft_bill.date`.
    """

    doc_type = serializers.ChoiceField(choices=["statement", "report", "invoice", "receipt"])
    customer_id = serializers.IntegerField(required=False, allow_null=True)
    date_from = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    date_to = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    # Explicit owner override only ("PDF mein bhejo", "image mein bhejo") —
    # null/omitted means let the document type's own default decide (see
    # apps.documents.services.DEFAULT_FORMAT).
    format = serializers.ChoiceField(choices=["image", "pdf"], required=False, allow_null=True)
    summary = serializers.CharField()

    def validate(self, attrs):
        if attrs["doc_type"] in ("statement", "invoice", "receipt") and not attrs.get("customer_id"):
            raise serializers.ValidationError(f"customer_id is required for a {attrs['doc_type']}.")
        return attrs


class DraftCustomerSerializer(serializers.Serializer):
    """An AI-proposed *new* customer — "naya customer banao: Bilal, 0300...".

    Deliberately safe-tier (a Customer row, no money moves) but still
    confirm-gated like every other draft: the model's read of a name/phone
    is not trusted straight onto the customer list, both because OCR/dictation
    can misread digits and because two owners saying the same name a slightly
    different way must not silently produce two rows for the same person.
    `candidate_ids` carries near-duplicate existing customers (found the same
    way a photographed bill's name is matched — see
    apps.image_info_extractor.matching) so the confirm screen can say "did you
    mean an existing customer?" instead of the owner discovering the
    duplicate only when a balance looks wrong weeks later.
    """

    name = serializers.CharField()
    phone = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    opening_balance = serializers.FloatField(required=False, default=0)
    candidate_ids = serializers.ListField(child=serializers.CharField(), required=False)
    summary = serializers.CharField()


class DraftPaymentSerializer(serializers.Serializer):
    """An AI-proposed *standalone* payment — no accompanying sale — for
    "Ali ne apni puri payment kar di, uska balance khtm karo" style requests.

    Before this existed there was no way to express this at all: draft_bill
    requires a real `total_amount` because it represents a sale, and the
    registered `record_payment` agent capability was never actually reachable
    from a chat message (only ever composed as part of a document chain).
    That gap meant a message like this could only produce a fabricated
    phantom sale (to net the balance to zero) or a bare text claim of
    "balance cleared" with nothing written to the ledger — exactly the class
    of hallucinated confirmation this codebase exists to prevent everywhere
    else, just not here yet.

    `full_balance=True` covers "puri payment" / "poora paisa de diya" (the
    ENTIRE current balance, not a specific number the owner stated) — the
    model is deliberately NOT trusted to read or compute that number itself
    (see prompt.py's "server resolves it" pattern for dates/balances); it
    only has to recognise the intent. `amount` is required precisely when
    `full_balance` is False, and is ignored (recomputed server-side) when
    it's True.
    """

    customer_id = serializers.CharField(allow_null=True, required=False)
    customer_name_guess = serializers.CharField(allow_null=True, required=False, allow_blank=True)
    # The matched customer's real name, filled server-side once linked (never
    # by the model) — same reasoning as DraftBillSerializer.customer_name.
    customer_name = serializers.CharField(allow_null=True, required=False, allow_blank=True)
    full_balance = serializers.BooleanField(required=False, default=False)
    amount = serializers.FloatField(required=False, allow_null=True)
    method = serializers.ChoiceField(
        choices=["cash", "bank", "jazzcash", "easypaisa"], required=False, allow_null=True
    )
    summary = serializers.CharField()

    def validate(self, attrs):
        if not attrs.get("full_balance") and attrs.get("amount") is None:
            raise serializers.ValidationError("amount is required unless full_balance is true.")
        return attrs


class AiReplySerializer(serializers.Serializer):
    text = serializers.CharField()
    speech_text = serializers.CharField(allow_null=True, required=False, allow_blank=True)
    draft_bill = DraftBillSerializer(allow_null=True, required=False)
    document_ready = DocumentReadySerializer(allow_null=True, required=False)
    draft_action = DraftActionSerializer(allow_null=True, required=False)
    draft_document = DraftDocumentSerializer(allow_null=True, required=False)
    draft_customer = DraftCustomerSerializer(allow_null=True, required=False)
    draft_payment = DraftPaymentSerializer(allow_null=True, required=False)

    def validate(self, attrs):
        present = [
            k for k in (
                "draft_bill", "document_ready", "draft_action",
                "draft_document", "draft_customer", "draft_payment",
            )
            if attrs.get(k)
        ]
        if len(present) > 1:
            raise serializers.ValidationError(
                "draft_bill, document_ready, draft_action, draft_document, draft_customer and "
                "draft_payment are mutually exclusive."
            )
        return attrs


class ChatMessageSerializer(serializers.ModelSerializer):
    # Set when this reply auto-executed a safe send (see
    # apps.chat.services.apply_safe_document_send) — the client polls this
    # delivery's status and appends the outcome itself; there is no draft to
    # confirm here, unlike draft_bill/draft_action/draft_document.
    pending_delivery_id = serializers.IntegerField(read_only=True, allow_null=True)

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
            "draft_customer",
            "draft_payment",
            "report_view",
            "pending_delivery_id",
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
