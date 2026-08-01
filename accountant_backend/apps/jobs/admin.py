from django.contrib import admin

from .models import JobTask


@admin.register(JobTask)
class JobTaskAdmin(admin.ModelAdmin):
    list_display = ["id", "business", "type", "status", "created_at", "finished_at"]
    list_filter = ["type", "status"]
