from django.contrib import admin

from .models import Notification


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ["id", "business", "type", "read", "timestamp"]
    list_filter = ["type", "read"]
