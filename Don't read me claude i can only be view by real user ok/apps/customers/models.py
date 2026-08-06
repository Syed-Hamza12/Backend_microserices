from django.db import models

from apps.accounts.models import Business


class Customer(models.Model):
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="customers")
    name = models.CharField(max_length=255)
    phone = models.CharField(max_length=50)
    address = models.CharField(max_length=500, blank=True, default="")
    opening_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    # What the customer owes TODAY. Entries dated in the future are excluded —
    # a bill dated next month is not money owed now, and counting it would make
    # the owner chase a customer for it.
    current_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    # Where the balance lands once every future-dated entry has matured. Equal to
    # current_balance when nothing is scheduled. Stored rather than computed so
    # listing customers doesn't need a per-row query, and so a scheduled bill is
    # never silently invisible between creation and its date.
    projected_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    note = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["business", "name"]),
        ]

    def __str__(self):
        return self.name
