from django.contrib import admin

from .models import DocumentDelivery


@admin.register(DocumentDelivery)
class DocumentDeliveryAdmin(admin.ModelAdmin):
    list_display = ("id", "business", "doc_type", "delivered_format", "to_phone", "status", "created_at")
    list_filter = ("status", "doc_type", "delivered_format")
    search_fields = ("to_phone", "business__business_name")
    readonly_fields = [f.name for f in DocumentDelivery._meta.fields]
