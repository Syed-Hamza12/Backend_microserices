from django.db import models

from apps.accounts.models import Business


class JobTask(models.Model):
    TYPE_CHOICES = [
        # Renders a document and sends it over WhatsApp. Replaces the old "pdf"
        # type, which generated a file, stored it, and returned a URL —
        # generated documents are transient now.
        ("document_send", "Document Send"),
        ("image_extract", "Image Extract"),
        ("voice_transcribe", "Voice Transcribe"),
    ]
    STATUS_CHOICES = [
        ("queued", "Queued"),
        ("processing", "Processing"),
        ("done", "Done"),
        ("failed", "Failed"),
    ]

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="job_tasks")
    type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="queued")
    payload = models.JSONField()
    result = models.JSONField(null=True, blank=True)
    error = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    # How many times a worker has claimed this job. Only ever above 1 when a
    # previous worker died mid-job and the job was requeued, so it doubles as
    # the guard that stops a job which reliably kills its worker from cycling
    # forever (see runworker.MAX_JOB_ATTEMPTS).
    attempts = models.PositiveSmallIntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "created_at"]),
        ]

    def __str__(self):
        return f"JobTask#{self.id} {self.type} {self.status}"
