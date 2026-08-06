from django.db import models

from apps.accounts.models import Business
from apps.customers.models import Customer
from apps.jobs.models import JobTask
from apps.sales.models import ActivityEntry

DOC_TYPE_CHOICES = [
    ("invoice", "Invoice"),
    ("statement", "Statement"),
    ("receipt", "Receipt"),
    ("report", "Report"),
]

FORMAT_CHOICES = [
    ("image", "Image"),
    ("pdf", "PDF"),
]


class DocumentDelivery(models.Model):
    """Audit record of a document sent to a customer over WhatsApp.

    Replaces the old `GeneratedDocument`, which stored a `file_url` pointing at
    a rendered PDF on disk. Generated files are transient now — rendered in
    memory, streamed to the Gateway, discarded — so a stored URL would refer to
    something that no longer exists. The permanent record is the business data
    the document was built from, plus this row saying what was sent, to whom,
    when, and whether it arrived.

    Regeneration is always possible from the source data, which is why nothing
    here tries to keep the file itself.
    """

    # Deliberately no "delivered" state.
    #
    # Baileys returns once WhatsApp has *accepted* the message; it exposes no
    # delivery or read receipt we act on. Calling that "Sent"/"Delivered" tells
    # the owner the customer has the bill when all we know is that WhatsApp took
    # it. `accepted` is the honest ceiling of what this system can observe, and
    # the app labels it that way.
    STATUS_CHOICES = [
        ("pending", "Queued"),
        ("sending", "Sending"),
        ("accepted", "Accepted by WhatsApp"),
        ("failed", "Failed"),
    ]

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="document_deliveries")
    job_task = models.ForeignKey(
        JobTask, on_delete=models.SET_NULL, null=True, blank=True, related_name="document_deliveries"
    )
    customer = models.ForeignKey(
        Customer, on_delete=models.SET_NULL, null=True, blank=True, related_name="document_deliveries"
    )
    doc_type = models.CharField(max_length=20, choices=DOC_TYPE_CHOICES)

    # What was asked for vs what actually went out. A bill too long to be
    # readable as an image is delivered as a PDF instead, and the audit trail
    # has to show what the customer actually received, not what was requested.
    requested_format = models.CharField(max_length=10, choices=FORMAT_CHOICES)
    delivered_format = models.CharField(max_length=10, choices=FORMAT_CHOICES, null=True, blank=True)

    # Denormalised deliberately: the phone number as dialled at send time. If
    # the customer's number is edited later, this must still show where the
    # document actually went.
    to_phone = models.CharField(max_length=50)

    related_entry = models.ForeignKey(
        ActivityEntry, on_delete=models.SET_NULL, null=True, blank=True, related_name="document_deliveries"
    )
    # Statement/report parameters, so a delivery can be reproduced exactly.
    parameters = models.JSONField(default=dict, blank=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    error_code = models.CharField(max_length=64, null=True, blank=True)
    error_message = models.TextField(null=True, blank=True)
    byte_size = models.PositiveIntegerField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    # When WhatsApp accepted the message — not when the customer received it.
    accepted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["business", "-created_at"]),
            models.Index(fields=["business", "status"]),
        ]
        verbose_name_plural = "document deliveries"

    def __str__(self):
        return f"{self.doc_type} -> {self.to_phone} ({self.status})"
