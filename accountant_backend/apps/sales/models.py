import uuid

from django.db import models
from django.utils import timezone

from apps.accounts.models import Business
from apps.customers.models import Customer


class ActivityEntry(models.Model):
    TYPE_CHOICES = [
        ("sale", "Sale"),
        ("payment", "Payment"),
    ]
    PAYMENT_METHOD_CHOICES = [
        ("cash", "Cash"),
        ("bank", "Bank"),
        ("jazzcash", "JazzCash"),
        ("easypaisa", "EasyPaisa"),
    ]
    CREATED_BY_CHOICES = [
        ("manual", "Manual"),
        ("ai_chat", "AI Chat"),
    ]

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="activity_entries")
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="activity_entries")
    type = models.CharField(max_length=10, choices=TYPE_CHOICES)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    balance_after = models.DecimalField(max_digits=12, decimal_places=2)
    timestamp = models.DateTimeField()
    sale_group_id = models.UUIDField(null=True, blank=True, default=None)
    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHOD_CHOICES, null=True, blank=True)
    note = models.TextField(null=True, blank=True)
    created_by = models.CharField(max_length=10, choices=CREATED_BY_CHOICES, default="manual")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["business", "customer", "timestamp"]),
        ]
        ordering = ["timestamp", "id"]

    def __str__(self):
        return f"{self.type} {self.amount} for {self.customer_id}"


class SaleLineItem(models.Model):
    entry = models.ForeignKey(ActivityEntry, on_delete=models.CASCADE, related_name="line_items")
    item_name = models.CharField(max_length=255)
    quantity = models.DecimalField(max_digits=12, decimal_places=2)
    rate = models.DecimalField(max_digits=12, decimal_places=2)

    @property
    def amount(self):
        return self.quantity * self.rate


class EntryChangeLog(models.Model):
    """Audit trail for AI-initiated edits/transfers (business_logic's
    "AI can modify money records, not just create them" case) — a dispute
    ("the AI changed my ledger") must be provable after the fact. Not used
    for manual (non-AI) edits, which are lower-risk/self-evident to the
    person who just made them.
    """

    ACTION_CHOICES = [("create", "Create"), ("edit", "Edit"), ("transfer", "Transfer")]

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="entry_change_logs")
    # Plain int, not a FK: the audit record must survive even if the entry
    # itself is later deleted through some other path.
    entry_id = models.PositiveIntegerField()
    action = models.CharField(max_length=10, choices=ACTION_CHOICES)
    old_values = models.JSONField()
    new_values = models.JSONField()
    source = models.CharField(max_length=20, default="ai_chat")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.action} on entry {self.entry_id} ({self.source})"


class PendingUndo(models.Model):
    """A short-lived, one-time-use revert token for an AI-confirmed
    edit/transfer — reduces the damage of a wrong confirm without needing
    a full generic undo-history system yet (deliberately scoped to just
    AI-initiated edit/transfer actions for now)."""

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="pending_undos")
    entry_id = models.PositiveIntegerField()
    action = models.CharField(max_length=10, choices=EntryChangeLog.ACTION_CHOICES)
    snapshot = models.JSONField()
    expires_at = models.DateTimeField()
    used = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def is_valid(self):
        return not self.used and timezone.now() < self.expires_at
