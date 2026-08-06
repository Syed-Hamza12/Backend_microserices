from django.db import models

from apps.accounts.models import Business


class Notification(models.Model):
    TYPE_CHOICES = [
        ("invoice_sent", "Invoice Sent"),
        # A queued document send failed. Raised from the background worker,
        # which is the only way the owner learns about it — nobody is watching
        # a response by the time the send actually runs.
        ("document_failed", "Document Send Failed"),
        ("payment_received", "Payment Received"),
        ("whatsapp_disconnected", "WhatsApp Disconnected"),
        ("pending_payment_reminder", "Pending Payment Reminder"),
        ("daily_summary", "Daily Summary"),
    ]

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="notifications")
    type = models.CharField(max_length=30, choices=TYPE_CHOICES)
    payload = models.JSONField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)
    read = models.BooleanField(default=False)

    class Meta:
        ordering = ["-timestamp", "-id"]

    def __str__(self):
        return f"{self.type} for business {self.business_id}"
