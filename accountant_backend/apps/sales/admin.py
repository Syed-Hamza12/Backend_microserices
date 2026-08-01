from django.contrib import admin

from .models import ActivityEntry, EntryChangeLog, PendingUndo, SaleLineItem


class SaleLineItemInline(admin.TabularInline):
    model = SaleLineItem
    extra = 0


@admin.register(ActivityEntry)
class ActivityEntryAdmin(admin.ModelAdmin):
    list_display = ["id", "customer", "type", "amount", "balance_after", "timestamp"]
    list_filter = ["business", "type"]
    inlines = [SaleLineItemInline]


@admin.register(EntryChangeLog)
class EntryChangeLogAdmin(admin.ModelAdmin):
    list_display = ["id", "business", "entry_id", "action", "source", "created_at"]
    list_filter = ["business", "action", "source"]
    readonly_fields = ["business", "entry_id", "action", "old_values", "new_values", "source", "created_at"]


@admin.register(PendingUndo)
class PendingUndoAdmin(admin.ModelAdmin):
    list_display = ["id", "business", "entry_id", "action", "used", "expires_at", "created_at"]
    list_filter = ["business", "action", "used"]
