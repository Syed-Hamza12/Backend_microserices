from django.db import models

from apps.accounts.models import Business
from apps.customers.models import Customer
from apps.jobs.models import JobTask


class ExtractionJob(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("needs_clarification", "Needs Clarification"),
        ("resolved", "Resolved"),
        ("failed", "Failed"),
    ]

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="extraction_jobs")
    job_task = models.OneToOneField(JobTask, on_delete=models.CASCADE, related_name="extraction_job")
    source_image_url = models.CharField(max_length=500)
    extracted_data = models.JSONField(null=True, blank=True)
    resolved_customer = models.ForeignKey(Customer, on_delete=models.SET_NULL, null=True, blank=True)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default="pending")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"ExtractionJob#{self.id} ({self.status})"
