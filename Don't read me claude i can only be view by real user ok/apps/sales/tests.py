"""Regression tests for the ledger's money invariants.

Each test here corresponds to a bug that was live in this code: negative
amounts being accepted, a sale+payment pair being half-deleted, undo losing
Decimal precision or replaying, and AI-proposed values reaching the ledger
unvalidated. They exist so those specific failures can't come back quietly.
"""

from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APITestCase

from apps.accounts.models import Business, User
from apps.customers.models import Customer

from . import services
from .models import ActivityEntry, EntryChangeLog
from .serializers import RecordPaymentSerializer, RecordSaleSerializer


class LedgerTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="o@x.com", email="o@x.com", password="pw")
        self.business = Business.objects.create(owner=self.user, business_name="Test Shop")
        self.customer = Customer.objects.create(
            business=self.business,
            name="Ali Raza",
            phone="923001112222",
            opening_balance=Decimal("0"),
            current_balance=Decimal("0"),
        )


class DashboardSummaryTests(APITestCase):
    """The Dashboard had no endpoint at all and shipped showing hardcoded
    sample figures, so a real owner's Home tab displayed another business's
    numbers."""

    def setUp(self):
        self.user = User.objects.create_user(username="d@x.com", email="d@x.com", password="pw")
        self.business = Business.objects.create(owner=self.user, business_name="Test Shop")
        self.customer = Customer.objects.create(
            business=self.business, name="Ali", phone="923001112222",
            opening_balance=Decimal("0"), current_balance=Decimal("0"),
        )
        self.client.force_authenticate(user=self.user)

    def _get(self):
        return self.client.get("/api/dashboard/summary/")

    def test_a_business_with_no_records_reports_zeroes_and_no_activity(self):
        response = self._get()
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertEqual(Decimal(str(data["todays_sales"])), Decimal("0"))
        self.assertEqual(Decimal(str(data["total_receivable"])), Decimal("0"))
        self.assertEqual(data["recent_activity"], [])

    def test_todays_sales_and_payments_are_totalled(self):
        services.record_sale(
            business=self.business, customer=self.customer,
            items=[{"item_name": "Rice", "quantity": Decimal("2"), "rate": Decimal("500")}],
            amount_received=Decimal("300"), payment_method="cash",
        )
        data = self._get().json()["data"]
        self.assertEqual(Decimal(str(data["todays_sales"])), Decimal("1000.00"))
        self.assertEqual(Decimal(str(data["todays_payments_received"])), Decimal("300.00"))
        # 1000 owed less 300 paid.
        self.assertEqual(Decimal(str(data["total_receivable"])), Decimal("700.00"))

    def test_a_customer_in_credit_does_not_cancel_out_another_customers_debt(self):
        """Summing raw balances would let one customer's overpayment hide what
        another owes, understating the shop's receivables."""
        in_credit = Customer.objects.create(
            business=self.business, name="Bilal", phone="923001112223",
            opening_balance=Decimal("0"), current_balance=Decimal("-5000"),
        )
        Customer.objects.filter(pk=self.customer.pk).update(current_balance=Decimal("2000"))

        data = self._get().json()["data"]
        self.assertEqual(Decimal(str(data["total_receivable"])), Decimal("2000.00"))
        self.assertEqual(in_credit.current_balance, Decimal("-5000"))

    def test_another_businesss_records_are_never_included(self):
        other_user = User.objects.create_user(username="x@x.com", email="x@x.com", password="pw")
        other_business = Business.objects.create(owner=other_user, business_name="Other Shop")
        other_customer = Customer.objects.create(
            business=other_business, name="Zain", phone="923001112224",
            opening_balance=Decimal("0"), current_balance=Decimal("0"),
        )
        services.record_sale(
            business=other_business, customer=other_customer,
            items=[{"item_name": "Sugar", "quantity": Decimal("1"), "rate": Decimal("9999")}],
        )

        data = self._get().json()["data"]
        self.assertEqual(Decimal(str(data["todays_sales"])), Decimal("0"))
        self.assertEqual(data["recent_activity"], [])

    def test_recent_activity_is_newest_first_and_carries_line_items(self):
        services.record_sale(
            business=self.business, customer=self.customer,
            items=[{"item_name": "Rice", "quantity": Decimal("2"), "rate": Decimal("500")}],
        )
        services.record_payment(
            business=self.business, customer=self.customer,
            amount=Decimal("200"), method="cash",
        )

        rows = self._get().json()["data"]["recent_activity"]
        self.assertEqual(rows[0]["type"], "payment")
        self.assertEqual(rows[1]["type"], "sale")
        self.assertEqual(rows[1]["line_items"][0]["item_name"], "Rice")
        self.assertEqual(rows[1]["customer_name"], "Ali")


class MoneyValidationTests(LedgerTestCase):
    def test_negative_quantity_rejected(self):
        serializer = RecordSaleSerializer(
            data={"customer_id": self.customer.id, "items": [{"item_name": "x", "quantity": "-2", "rate": "100"}]}
        )
        self.assertFalse(serializer.is_valid())

    def test_negative_rate_rejected(self):
        serializer = RecordSaleSerializer(
            data={"customer_id": self.customer.id, "items": [{"item_name": "x", "quantity": "2", "rate": "-100"}]}
        )
        self.assertFalse(serializer.is_valid())

    def test_negative_payment_rejected(self):
        serializer = RecordPaymentSerializer(
            data={"customer_id": self.customer.id, "amount": "-500", "payment_method": "cash"}
        )
        self.assertFalse(serializer.is_valid())

    def test_payment_larger_than_sale_total_rejected(self):
        serializer = RecordSaleSerializer(
            data={
                "customer_id": self.customer.id,
                "items": [{"item_name": "x", "quantity": "1", "rate": "100"}],
                "amount_received": "500",
                "payment_method": "cash",
            }
        )
        self.assertFalse(serializer.is_valid())

    def test_valid_sale_still_accepted(self):
        serializer = RecordSaleSerializer(
            data={"customer_id": self.customer.id, "items": [{"item_name": "x", "quantity": "2", "rate": "100"}]}
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)


class BalanceTests(LedgerTestCase):
    def test_sale_and_payment_produce_correct_balance(self):
        services.record_sale(
            business=self.business,
            customer=self.customer,
            items=[{"item_name": "Rice", "quantity": Decimal("2"), "rate": Decimal("500")}],
            amount_received=Decimal("400"),
            payment_method="cash",
        )
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.current_balance, Decimal("600"))

    def test_changing_opening_balance_recalculates_the_ledger(self):
        services.record_sale(
            business=self.business,
            customer=self.customer,
            items=[{"item_name": "Rice", "quantity": Decimal("1"), "rate": Decimal("100")}],
        )
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.current_balance, Decimal("100"))

        self.customer.opening_balance = Decimal("50")
        self.customer.save(update_fields=["opening_balance"])
        services.recalculate_balances(self.customer)

        self.customer.refresh_from_db()
        self.assertEqual(self.customer.current_balance, Decimal("150"))
        self.assertEqual(ActivityEntry.objects.get(customer=self.customer).balance_after, Decimal("150"))


class GroupedEntryTests(LedgerTestCase):
    def test_deleting_a_sale_also_removes_its_linked_payment(self):
        sale, payment = services.record_sale(
            business=self.business,
            customer=self.customer,
            items=[{"item_name": "Rice", "quantity": Decimal("2"), "rate": Decimal("500")}],
            amount_received=Decimal("400"),
            payment_method="cash",
        )
        self.assertIsNotNone(payment)
        self.assertEqual(ActivityEntry.objects.filter(business=self.business).count(), 2)

        services.delete_entry(entry=sale)

        # The orphaned payment used to survive here, leaving an unexplained
        # credit on the customer's balance.
        self.assertEqual(ActivityEntry.objects.filter(business=self.business).count(), 0)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.current_balance, Decimal("0"))


class UndoTests(LedgerTestCase):
    def _payment(self, amount):
        return services.record_payment(
            business=self.business, customer=self.customer, amount=amount, method="cash"
        )

    def test_undo_restores_the_exact_decimal_amount(self):
        entry = self._payment(Decimal("0.10"))
        entry, undo = services.edit_entry_via_ai(entry=entry, changes={"amount": 0.30})
        entry.refresh_from_db()
        self.assertEqual(entry.amount, Decimal("0.30"))

        services.undo_pending_action(pending_undo=undo)
        entry.refresh_from_db()
        # Restoring through float() used to reintroduce binary rounding error.
        self.assertEqual(entry.amount, Decimal("0.10"))

    def test_undo_cannot_be_applied_twice(self):
        entry = self._payment(Decimal("100"))
        entry, undo = services.edit_entry_via_ai(entry=entry, changes={"amount": 250})
        services.undo_pending_action(pending_undo=undo)
        with self.assertRaises(ValueError):
            services.undo_pending_action(pending_undo=undo)

    def test_undo_of_a_deleted_entry_raises_a_handled_error(self):
        entry = self._payment(Decimal("100"))
        entry, undo = services.edit_entry_via_ai(entry=entry, changes={"amount": 250})
        ActivityEntry.objects.filter(pk=entry.pk).delete()
        # Previously surfaced as an unhandled DoesNotExist (HTTP 500).
        with self.assertRaises(ValueError):
            services.undo_pending_action(pending_undo=undo)


class AiProposedChangeTests(LedgerTestCase):
    def _payment(self):
        return services.record_payment(
            business=self.business, customer=self.customer, amount=Decimal("50"), method="cash"
        )

    def test_invalid_ai_amounts_are_rejected(self):
        for bad_amount in ["not-a-number", -5, 0, 10**15]:
            with self.subTest(amount=bad_amount):
                with self.assertRaises(ValueError):
                    services.edit_entry_via_ai(entry=self._payment(), changes={"amount": bad_amount})

    def test_malformed_ai_line_items_are_rejected(self):
        sale, _ = services.record_sale(
            business=self.business,
            customer=self.customer,
            items=[{"item_name": "Rice", "quantity": Decimal("1"), "rate": Decimal("100")}],
        )
        for bad_items in [
            [{"item_name": "x"}],
            [{"item_name": "x", "quantity": "abc", "rate": 10}],
            [{"item_name": "", "quantity": 1, "rate": 10}],
            ["not-a-dict"],
            [],
        ]:
            with self.subTest(items=bad_items):
                with self.assertRaises(ValueError):
                    services.edit_entry_via_ai(entry=sale, changes={"items": bad_items})

    def test_ai_created_sale_is_audited(self):
        sale, _ = services.record_sale(
            business=self.business,
            customer=self.customer,
            items=[{"item_name": "Rice", "quantity": Decimal("1"), "rate": Decimal("100")}],
            created_by="ai_chat",
        )
        services.log_ai_created_sale(entry=sale, source_message_id=42)

        log = EntryChangeLog.objects.get(entry_id=sale.id, action="create")
        self.assertEqual(log.source, "ai_chat")
        self.assertEqual(log.new_values["source_message_id"], 42)
