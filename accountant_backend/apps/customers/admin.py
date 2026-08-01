from django.contrib import admin

from .models import Customer


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ["name", "business", "phone", "current_balance", "created_at"]
    list_filter = ["business"]
    search_fields = ["name", "phone"]
