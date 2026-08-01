from django.db import models
from django.utils import timezone

from apps.accounts.models import Business

FEATURE_KEY_CHOICES = [
    ("ai_chat", "AI Chat"),
    ("voice_reply", "Voice Reply"),
    ("image_extraction", "Image Extraction"),
    ("whatsapp_send", "WhatsApp Send"),
]


class Plan(models.Model):
    name = models.CharField(max_length=100)
    price_pkr = models.DecimalField(max_digits=12, decimal_places=2)
    is_custom = models.BooleanField(default=False)
    billing_period = models.CharField(max_length=20, default="monthly")
    chat_history_limit = models.PositiveIntegerField(default=15)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name

    def has_feature(self, feature_key):
        return self.features.filter(feature_key=feature_key, enabled=True).exists()

    def feature_cap(self, feature_key):
        feature = self.features.filter(feature_key=feature_key, enabled=True).first()
        return feature.monthly_cap if feature else None


class PlanFeature(models.Model):
    plan = models.ForeignKey(Plan, on_delete=models.CASCADE, related_name="features")
    feature_key = models.CharField(max_length=50, choices=FEATURE_KEY_CHOICES)
    enabled = models.BooleanField(default=True)
    monthly_cap = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        unique_together = ("plan", "feature_key")

    def __str__(self):
        return f"{self.plan.name} - {self.feature_key}"


class Subscription(models.Model):
    STATUS_CHOICES = [
        ("active", "Active"),
        ("expired", "Expired"),
        ("cancelled", "Cancelled"),
    ]

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="subscriptions")
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="subscriptions")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="active")
    started_at = models.DateTimeField()
    expires_at = models.DateTimeField(null=True, blank=True)
    is_manual_override = models.BooleanField(default=False)
    chat_history_limit_override = models.PositiveIntegerField(null=True, blank=True)
    notes = models.TextField(null=True, blank=True)

    def __str__(self):
        return f"{self.business.business_name} - {self.plan.name} ({self.status})"

    def is_currently_active(self):
        """Active *and* not past its expiry date.

        `status` alone was the whole test, and nothing in the system ever
        flips it — so a subscription with an expires_at last year kept every
        paid feature switched on indefinitely. Checking the date here means
        expiry works even if the sweep command hasn't run.
        """
        if self.status != "active":
            return False
        return self.expires_at is None or self.expires_at > timezone.now()

    def has_feature(self, feature_key):
        return self.is_currently_active() and self.plan.has_feature(feature_key)

    def resolved_chat_history_limit(self):
        return self.chat_history_limit_override or self.plan.chat_history_limit

    @classmethod
    def active_for(cls, business):
        return (
            cls.objects.filter(business=business, status="active")
            .filter(models.Q(expires_at__isnull=True) | models.Q(expires_at__gt=timezone.now()))
            .order_by("-started_at")
            .first()
        )

    @classmethod
    def business_has_feature(cls, business, feature_key):
        subscription = cls.active_for(business)
        if subscription is None:
            return False
        return subscription.has_feature(feature_key)


class UsageCounter(models.Model):
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="usage_counters")
    feature_key = models.CharField(max_length=50, choices=FEATURE_KEY_CHOICES)
    period_start = models.DateField()
    count = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ("business", "feature_key", "period_start")

    def __str__(self):
        return f"{self.business_id} {self.feature_key} {self.period_start}: {self.count}"
