from django.contrib import admin

from .models import ExtractionJob


@admin.register(ExtractionJob)
class ExtractionJobAdmin(admin.ModelAdmin):
    list_display = ["id", "business", "status", "resolved_customer", "created_at"]
    list_filter = ["status"]
