from django.db import models

from apps.accounts.models import Business
from apps.documents.models import DocumentDelivery


class ChatSyncState(models.Model):
    """Per-business marker that tells every device when history was wiped.

    Delta sync can only report rows that still exist, so a device that synced
    yesterday has no way to learn that a conversation was *deleted* — it would
    keep showing history the business has already erased, on any phone that
    happened to be offline at the time.

    A `history_cleared_at` marker solves that without tombstones: clients store
    the value they last saw, and a newer one means "drop your cache and start
    over". Tombstone rows would work too, but they would keep the deleted
    messages on the server indefinitely, which is precisely what "clear my chat
    history" is asking not to happen.
    """

    business = models.OneToOneField(Business, on_delete=models.CASCADE, related_name="chat_sync_state")
    history_cleared_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"chat sync state for business {self.business_id}"

    @classmethod
    def cleared_at_for(cls, business):
        state = cls.objects.filter(business=business).first()
        return state.history_cleared_at if state else None


class Conversation(models.Model):
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="conversations")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Conversation#{self.id} for business {self.business_id}"


class ChatMessage(models.Model):
    SENDER_CHOICES = [
        ("owner", "Owner"),
        ("ai", "AI"),
    ]

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="messages")
    sender = models.CharField(max_length=10, choices=SENDER_CHOICES)
    text = models.TextField(null=True, blank=True)
    speech_text = models.TextField(null=True, blank=True)
    image_url = models.URLField(null=True, blank=True)
    draft_bill = models.JSONField(null=True, blank=True)
    draft_confirmed = models.BooleanField(default=False)
    document_ready = models.JSONField(null=True, blank=True)
    # AI-proposed edit/transfer of an *existing* ActivityEntry — see
    # apps.chat.serializers.DraftActionSerializer for the shape and
    # apps.sales.services.edit_entry_via_ai/transfer_entry for what
    # confirming one actually does. Mutually exclusive with draft_bill/
    # document_ready, same "only one of three, or none" rule.
    draft_action = models.JSONField(null=True, blank=True)
    # AI-proposed statement/report over a date range — see
    # apps.chat.serializers.DraftDocumentSerializer. Confirmed by the owner like
    # any other draft; the server builds it from the ledger.
    draft_document = models.JSONField(null=True, blank=True)
    # AI-proposed *new* customer — see apps.chat.serializers.DraftCustomerSerializer.
    # Mutually exclusive with the three drafts above, same "only one of these,
    # or none" rule, and confirmed the same way (ConfirmDraftCustomerView).
    draft_customer = models.JSONField(null=True, blank=True)
    # A date/date-range the owner asked about across MULTIPLE customers
    # ("pichle hafte ki detail batao", "10 se 20 tareek ka hisaab") — set
    # deterministically by apps.chat.services (never by the model, same
    # reasoning as every other date fact in this codebase), whenever such a
    # range was recognised in the owner's message. Purely informational: no
    # confirm step, nothing it produces changes the ledger, so it can coexist
    # with a draft_bill/draft_action/draft_document/draft_customer on the same
    # message rather than being mutually exclusive with them. The mobile app
    # renders a "View" button that fetches the matching entries directly
    # (GET /sales/entries/?date_from=&date_to=) rather than trusting anything
    # summarized in "text".
    report_view = models.JSONField(null=True, blank=True)
    # Set when this reply auto-executed a safe capability that queued a
    # WhatsApp send (see apps.agent.executor) — the AI already acted, so this
    # is not another draft to confirm. The client polls this delivery's status
    # and appends the outcome as a follow-up plain-text bubble once it
    # resolves; see apps.agent.goals.GoalManager.handle_event.
    pending_delivery = models.ForeignKey(
        DocumentDelivery, on_delete=models.SET_NULL, null=True, blank=True, related_name="chat_messages"
    )
    # True when this AI turn is the "sorry, I couldn't process that" fallback
    # rather than a real model reply. Kept out of prompt history (see
    # apps.chat.services._recent_history) and lets the app show a retry
    # affordance instead of presenting an outage as an answer.
    is_error_fallback = models.BooleanField(default=False)
    timestamp = models.DateTimeField(auto_now_add=True)
    # Bumped whenever the row changes, not just when it is created. The mobile
    # cache syncs on this: `draft_confirmed` flipping is a change the phone must
    # see, and an id- or created-time cursor would never notice it — leaving a
    # draft looking un-confirmed offline after it had already been recorded.
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["timestamp", "id"]
        indexes = [
            # The delta-sync query: everything in this conversation touched
            # since the client last looked.
            models.Index(fields=["conversation", "updated_at"]),
        ]

    def __str__(self):
        return f"{self.sender}: {(self.text or '')[:40]}"
