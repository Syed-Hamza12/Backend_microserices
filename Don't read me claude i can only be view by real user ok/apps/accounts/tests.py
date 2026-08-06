from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APITestCase

from .models import Business, User


class BusinessTypeChoicesViewTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="bt@x.com", email="bt@x.com", password="pw")
        self.client.force_authenticate(user=self.user)

    def test_returns_the_full_fixed_list(self):
        response = self.client.get("/api/business/types/")
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        codes = {row["code"] for row in data}
        # Spot-check a few — every code returned here must have a real
        # domain markdown file or be a deliberately documented gap, since
        # this is the exact list the mobile picker offers.
        self.assertIn("HARDWARE", codes)
        self.assertIn("TAILOR", codes)
        self.assertIn("OTHER", codes)
        self.assertEqual(len(codes), len(Business.BUSINESS_TYPE_CHOICES))


class BusinessProfileTypeAndInstructionsTests(APITestCase):
    """business_type and special_instructions round-trip through the same
    create/update endpoint every other profile field already uses — no new
    endpoint needed, per the existing BusinessProfileView pattern."""

    def setUp(self):
        self.user = User.objects.create_user(username="bp@x.com", email="bp@x.com", password="pw")
        self.client.force_authenticate(user=self.user)

    def test_create_sets_business_type_and_instructions(self):
        response = self.client.post(
            "/api/business/profile/",
            {
                "business_name": "Kashan Hardware",
                "business_type": "HARDWARE",
                "special_instructions": "We sell only in dozens.",
                "currency_code": "PKR",
                "language": "en",
            },
        )
        self.assertEqual(response.status_code, 201, response.content)
        business = Business.objects.get(owner=self.user)
        self.assertEqual(business.business_type, "HARDWARE")
        self.assertEqual(business.special_instructions, "We sell only in dozens.")

    def test_patch_updates_business_type(self):
        business = Business.objects.create(owner=self.user, business_name="Test Shop")
        response = self.client.patch("/api/business/profile/", {"business_type": "GROCERY"})
        self.assertEqual(response.status_code, 200, response.content)
        business.refresh_from_db()
        self.assertEqual(business.business_type, "GROCERY")

    def test_blank_business_type_is_allowed(self):
        response = self.client.post(
            "/api/business/profile/",
            {"business_name": "No Type Yet", "currency_code": "PKR", "language": "en"},
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(Business.objects.get(owner=self.user).business_type, "")
