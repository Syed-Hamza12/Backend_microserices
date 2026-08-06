from django.contrib import admin

from .models import Plan, PlanFeature, Subscription, UsageCounter


class PlanFeatureInline(admin.TabularInline):
    model = PlanFeature
    extra = 0


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = ["name", "price_pkr", "is_custom", "chat_history_limit"]
    inlines = [PlanFeatureInline]


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ["business", "plan", "status", "started_at", "expires_at", "is_manual_override"]
    list_filter = ["status", "plan", "is_manual_override"]


@admin.register(UsageCounter)
class UsageCounterAdmin(admin.ModelAdmin):
    list_display = ["business", "feature_key", "period_start", "count"]
    list_filter = ["feature_key"]
