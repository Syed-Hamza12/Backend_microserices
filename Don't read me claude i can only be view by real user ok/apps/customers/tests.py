from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APITestCase

from apps.accounts.models import Business, User

from .models import Customer
from .phone import normalize_phone
from .serializers import CustomerSerializer


class NormalizePhoneTests(TestCase):
    """A number saved in Pakistan's local dialing format never resolves as
    a WhatsApp recipient — Baileys just times out looking for it, silently,
    on every single send attempt. See phone.py's module docstring."""

    def test_local_format_gets_the_country_code(self):
        self.assertEqual(normalize_phone("03339233158"), "923339233158")

    def test_already_international_is_left_alone(self):
        self.assertEqual(normalize_phone("923339233158"), "923339233158")

    def test_leading_plus_is_stripped(self):
        self.assertEqual(normalize_phone("+923339233158"), "923339233158")

    def test_spaces_and_dashes_are_stripped(self):
        self.assertEqual(normalize_phone("0333-923-3158"), "923339233158")
        self.assertEqual(normalize_phone("0333 923 3158"), "923339233158")

    def test_unrecognized_format_is_passed_through_not_guessed(self):
        # Not confidently a PK local number (wrong length) — never silently
        # rewritten, since a wrong guess would corrupt an already-correct
        # number for a different country.
        self.assertEqual(normalize_phone("123456"), "123456")

    def test_empty_or_none_is_left_alone(self):
        self.assertEqual(normalize_phone(""), "")
        self.assertIsNone(normalize_phone(None))


class CustomerSerializerPhoneNormalizationTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="c@x.com", email="c@x.com", password="pw")
        self.business = Business.objects.create(owner=self.user, business_name="Test Shop")

    def test_local_format_phone_is_normalized_on_save(self):
        serializer = CustomerSerializer(data={
            "name": "Ali", "phone": "03339233158", "opening_balance": "0",
        })
        self.assertTrue(serializer.is_valid(), serializer.errors)
        customer = serializer.save(business=self.business)
        self.assertEqual(customer.phone, "923339233158")

    def test_already_correct_phone_is_unaffected(self):
        serializer = CustomerSerializer(data={
            "name": "Ali", "phone": "923339233158", "opening_balance": "0",
        })
        self.assertTrue(serializer.is_valid(), serializer.errors)
        customer = serializer.save(business=self.business)
        self.assertEqual(customer.phone, "923339233158")
