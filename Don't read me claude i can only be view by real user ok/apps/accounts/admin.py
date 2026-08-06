from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import Business, User


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    model = User
    list_display = ["email", "username", "auth_provider", "is_staff"]
    ordering = ["email"]


@admin.register(Business)
class BusinessAdmin(admin.ModelAdmin):
    list_display = ["business_name", "owner", "currency_code", "language", "created_at"]
