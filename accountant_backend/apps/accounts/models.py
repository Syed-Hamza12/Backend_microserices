from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    AUTH_PROVIDER_CHOICES = [
        ("google", "Google"),
        ("email", "Email"),
    ]

    email = models.EmailField(unique=True)
    auth_provider = models.CharField(max_length=10, choices=AUTH_PROVIDER_CHOICES, default="email")
    google_sub = models.CharField(max_length=255, unique=True, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["username"]

    def __str__(self):
        return self.email


class Business(models.Model):
    LANGUAGE_CHOICES = [
        ("en", "English"),
        ("ur", "Urdu"),
        ("roman_ur", "Roman Urdu"),
    ]

    owner = models.OneToOneField(User, on_delete=models.CASCADE, related_name="business")
    business_name = models.CharField(max_length=255)
    business_category = models.CharField(max_length=255, blank=True, default="")
    address = models.CharField(max_length=255, blank=True, default="")
    phone = models.CharField(max_length=50, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    currency_code = models.CharField(max_length=10, default="PKR")
    logo_url = models.URLField(null=True, blank=True)
    language = models.CharField(max_length=10, choices=LANGUAGE_CHOICES, default="en")
    gateway_session_id = models.CharField(max_length=255, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.business_name
