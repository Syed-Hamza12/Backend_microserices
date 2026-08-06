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

    # (code, label) pairs — code is what's stored and what
    # apps.chat.domain_knowledge.BusinessDomainProvider maps to a markdown
    # file under docs/business_domains/<code>.md; label is what the mobile
    # app's business-type picker displays. Blank means "not set" — a
    # business created before this field existed, or one whose owner hasn't
    # picked a type — and the domain-knowledge layer degrades gracefully to
    # injecting nothing rather than guessing.
    BUSINESS_TYPE_CHOICES = [
        ("HARDWARE", "Hardware Store"),
        ("PAINT_STORE", "Paint Store"),
        ("ELECTRICAL", "Electrical Store"),
        ("GARMENTS_ACCESSORIES", "Garments Accessories"),
        ("GARMENTS", "Garments"),
        ("FABRIC", "Fabric Store"),
        ("TEXTILE", "Textile"),
        ("RICE_GRAIN", "Rice / Grain Dealer"),
        ("FLOUR_MILL", "Flour Mill"),
        ("GENERAL_STORE", "General Store"),
        ("GROCERY", "Grocery"),
        ("RESTAURANT", "Restaurant"),
        ("HOTEL", "Hotel"),
        ("BAKERY", "Bakery"),
        ("FAST_FOOD", "Fast Food"),
        ("TEA_SHOP", "Tea Shop"),
        ("MEDICAL_STORE", "Medical Store"),
        ("PHARMACY", "Pharmacy"),
        ("BIKE_WORKSHOP", "Bike Workshop"),
        ("BIKE_PARTS", "Bike Parts"),
        ("CAR_WORKSHOP", "Car Workshop"),
        ("TYRE_SHOP", "Tyre Shop"),
        ("CONSTRUCTION", "Construction Material"),
        ("STEEL", "Steel"),
        ("CEMENT", "Cement"),
        ("SANITARY", "Sanitary"),
        ("TILES", "Tiles"),
        ("PLASTIC", "Plastic"),
        ("WHOLESALE", "Wholesale"),
        ("RETAIL", "Retail"),
        ("MOBILE_SHOP", "Mobile Shop"),
        ("COMPUTER_SHOP", "Computer Shop"),
        ("ELECTRONICS", "Electronics"),
        ("FURNITURE", "Furniture"),
        ("PRINTING_PRESS", "Printing Press"),
        ("STATIONERY", "Stationery"),
        ("JEWELRY", "Jewelry"),
        ("COSMETICS", "Cosmetics"),
        ("BEAUTY_SALON", "Beauty Salon"),
        ("TAILOR", "Tailor"),
        ("LAUNDRY", "Laundry"),
        ("MILK_SHOP", "Milk Shop"),
        ("FRUIT_SHOP", "Fruit Shop"),
        ("VEGETABLE_SHOP", "Vegetable Shop"),
        ("POULTRY", "Poultry"),
        ("OTHER", "Other"),
    ]

    owner = models.OneToOneField(User, on_delete=models.CASCADE, related_name="business")
    business_name = models.CharField(max_length=255)
    business_category = models.CharField(max_length=255, blank=True, default="")
    # Structured business type, distinct from the free-text business_category
    # above — this is what the domain-knowledge layer keys off, so it has to
    # be one of a fixed, known set the backend has a matching document for
    # (or can gracefully have none for), not arbitrary owner-typed text.
    business_type = models.CharField(
        max_length=32, choices=BUSINESS_TYPE_CHOICES, blank=True, default=""
    )
    # Owner-written rules for how THIS business wants the AI to behave —
    # "we sell only in dozens", "never ask for phone number", "we call
    # invoices Slip". Sent with every message, and always overrides the
    # business-type domain defaults when the two disagree (see
    # apps.chat.prompt.build_system_prompt's context ordering).
    special_instructions = models.TextField(blank=True, default="")
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
