"""Regression tests for customer matching and OCR value parsing.

The matching threshold used to be loose enough (0.6, no runner-up check) that a
photographed bill could be attached to a different customer's ledger silently
and with confident wording. These tests pin the behaviour that replaced it:
match only when it's clear, otherwise ask.
"""

from decimal import Decimal

from django.test import TestCase

from apps.accounts.models import Business, User
from apps.customers.models import Customer

from .matching import find_matching_customer
from .services import _parse_amount, _parse_items


class CustomerMatchingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="o@x.com", email="o@x.com", password="pw")
        self.business = Business.objects.create(owner=self.user, business_name="Test Shop")

    def _customer(self, name):
        return Customer.objects.create(
            business=self.business, name=name, phone="923000000000", opening_balance=0, current_balance=0
        )

    def test_exact_name_matches(self):
        ali = self._customer("Ali Raza")
        matched, candidates = find_matching_customer(self.business, "Ali Raza")
        self.assertEqual(matched, ali)
        self.assertEqual(candidates, [])

    def test_exact_match_wins_even_beside_a_similar_name(self):
        ali = self._customer("Ali Raza")
        self._customer("Alina Raza")
        matched, _ = find_matching_customer(self.business, "Ali Raza")
        self.assertEqual(matched, ali)

    def test_dissimilar_name_does_not_match(self):
        self._customer("Ali Raza")
        matched, _ = find_matching_customer(self.business, "Adil")
        self.assertIsNone(matched)

    def test_ambiguous_name_asks_instead_of_guessing(self):
        self._customer("Ali Raza")
        self._customer("Ali Rana")
        matched, candidates = find_matching_customer(self.business, "Ali Rasa")
        # Picking either of two near-identical names would be a coin toss that
        # puts money on the wrong ledger.
        self.assertIsNone(matched)
        self.assertGreaterEqual(len(candidates), 2)

    def test_duplicate_names_are_ambiguous(self):
        self._customer("Ali Raza")
        self._customer("Ali Raza")
        matched, candidates = find_matching_customer(self.business, "Ali Raza")
        self.assertIsNone(matched)
        self.assertEqual(len(candidates), 2)

    def test_blank_name_matches_nothing(self):
        self._customer("Ali Raza")
        self.assertEqual(find_matching_customer(self.business, "")[0], None)
        self.assertEqual(find_matching_customer(self.business, None)[0], None)


class OcrParsingTests(TestCase):
    def test_amount_formats_that_should_parse(self):
        self.assertEqual(_parse_amount("1,200"), Decimal("1200.00"))
        self.assertEqual(_parse_amount("Rs 1200"), Decimal("1200.00"))
        self.assertEqual(_parse_amount(1200), Decimal("1200.00"))
        self.assertEqual(_parse_amount("1200.50"), Decimal("1200.50"))

    def test_unusable_amounts_return_none_instead_of_raising(self):
        # These used to reach float() outside the job's try block and kill the
        # job, leaving the owner with no reply at all.
        for bad in [None, "", "abc", "-", 0, -5, 10**15]:
            with self.subTest(value=bad):
                self.assertIsNone(_parse_amount(bad))

    def test_only_fully_readable_line_items_are_kept(self):
        items = _parse_items(
            [
                {"item_name": "Rice", "quantity": 2, "rate": 500},
                {"item_name": "Sugar", "quantity": None, "rate": 100},
                {"item_name": "", "quantity": 1, "rate": 50},
                "not-a-dict",
            ]
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["item_name"], "Rice")

    def test_non_list_items_are_tolerated(self):
        self.assertEqual(_parse_items(None), [])
        self.assertEqual(_parse_items("nonsense"), [])
