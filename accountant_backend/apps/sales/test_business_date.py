"""Business-date resolution, timezone correctness and edit protection."""

from datetime import date, timedelta, timezone as dt_timezone
from decimal import Decimal

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.accounts.models import Business, User
from apps.customers.models import Customer
from apps.documents.models import DocumentDelivery

from . import services as sales_services
from .business_date import (
    AmbiguousBusinessDateError,
    BusinessDateError,
    business_today,
    is_future,
    resolve,
    to_entry_timestamp,
)
from .models import ActivityEntry


class BusinessTimezoneTests(TestCase):
    def test_today_follows_the_business_timezone_not_utc(self):
        """The bug this fixes: with TIME_ZONE=UTC the day rolled over at 5am
        Pakistan time, so a 1am sale landed on the previous day's report and
        printed yesterday's date on the customer's invoice."""
        self.assertEqual(timezone.get_current_timezone_name(), "Asia/Karachi")
        # 2026-07-31 20:00 UTC is already 2026-08-01 01:00 in Karachi.
        moment = timezone.datetime(2026, 7, 31, 20, 0, tzinfo=dt_timezone.utc)
        self.assertEqual(timezone.localtime(moment).date(), date(2026, 8, 1))


class DateResolutionTests(TestCase):
    def setUp(self):
        self.today = business_today()

    def test_absent_date_means_today(self):
        self.assertIsNone(resolve(None))
        self.assertIsNone(resolve(""))

    def test_relative_phrases(self):
        self.assertEqual(resolve("today"), self.today)
        self.assertEqual(resolve("yesterday"), self.today - timedelta(days=1))
        self.assertEqual(resolve("3 days ago"), self.today - timedelta(days=3))

    def test_absolute_iso_date(self):
        target = self.today - timedelta(days=6)
        self.assertEqual(resolve(target.isoformat()), target)

    def test_weekday_phrases_resolve_into_the_past(self):
        resolved = resolve("last monday")
        self.assertLessEqual(resolved, self.today)
        self.assertEqual(resolved.weekday(), 0)

    def test_future_dates_are_supported(self):
        """A planned bill is a real scenario, not an error."""
        self.assertEqual(resolve("tomorrow"), self.today + timedelta(days=1))
        target = self.today + timedelta(days=15)
        self.assertEqual(resolve(target.isoformat()), target)

    def test_dates_far_outside_the_window_are_refused_in_both_directions(self):
        # Almost always a mistyped year: backwards it rewrites the ledger behind
        # it, forwards it sits un-matured and invisible for years.
        with self.assertRaises(BusinessDateError):
            resolve("2019-01-01")
        with self.assertRaises(BusinessDateError):
            resolve((self.today + timedelta(days=400)).isoformat())

    def test_kal_is_refused_as_ambiguous_rather_than_guessed(self):
        """In Urdu "kal" means both yesterday and tomorrow.

        With entries datable in both directions, picking one is a coin flip that
        lands money on the wrong side of today — so the owner is asked.
        """
        with self.assertRaises(AmbiguousBusinessDateError) as caught:
            resolve("kal")
        message = str(caught.exception)
        self.assertIn("yesterday", message)
        self.assertIn("tomorrow", message)

    def test_unparseable_input_is_refused_not_guessed(self):
        with self.assertRaises(BusinessDateError):
            resolve("sometime last spring")

    def test_timestamp_keeps_time_of_day_for_ordering(self):
        target = self.today - timedelta(days=2)
        stamp = to_entry_timestamp(target)
        self.assertEqual(timezone.localtime(stamp).date(), target)
        # Not midnight: several entries backdated in one sitting must keep the
        # order they were entered, which the time component provides.
        self.assertNotEqual((stamp.hour, stamp.minute, stamp.second), (0, 0, 0))


class BackdatedLedgerTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="d@x.com", email="d@x.com", password="pw")
        self.business = Business.objects.create(owner=self.user, business_name="Hamza Traders")
        self.customer = Customer.objects.create(
            business=self.business, name="Ali", phone="923000000000",
            opening_balance=Decimal("0"), current_balance=Decimal("0"),
        )

    def test_a_backdated_entry_is_inserted_in_date_order_and_balances_stay_correct(self):
        today = business_today()
        sales_services.record_payment(
            business=self.business, customer=self.customer,
            amount=Decimal("100"), method="cash",
        )
        # Recorded second, but dated earlier — it must sort before the first.
        sales_services.record_sale(
            business=self.business, customer=self.customer,
            items=[{"item_name": "Rice", "quantity": Decimal("1"), "rate": Decimal("500")}],
            date=to_entry_timestamp(today - timedelta(days=3)),
        )

        entries = list(
            ActivityEntry.objects.filter(customer=self.customer).order_by("timestamp", "id")
        )
        self.assertEqual([e.type for e in entries], ["sale", "payment"])
        # 500 sale then 100 payment = 400, regardless of insertion order.
        self.assertEqual(entries[0].balance_after, Decimal("500"))
        self.assertEqual(entries[1].balance_after, Decimal("400"))
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.current_balance, Decimal("400"))

    def test_created_at_stays_the_system_clock(self):
        """The business date is the ledger date; created_at is the audit trail."""
        backdated = business_today() - timedelta(days=5)
        sale, _ = sales_services.record_sale(
            business=self.business, customer=self.customer,
            items=[{"item_name": "Rice", "quantity": Decimal("1"), "rate": Decimal("100")}],
            date=to_entry_timestamp(backdated),
        )
        self.assertEqual(timezone.localtime(sale.timestamp).date(), backdated)
        self.assertEqual(timezone.localtime(sale.created_at).date(), business_today())


class EntryEditProtectionTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="e@x.com", email="e@x.com", password="pw")
        self.business = Business.objects.create(owner=self.user, business_name="Hamza Traders")
        self.customer = Customer.objects.create(
            business=self.business, name="Ali", phone="923000000000",
            opening_balance=Decimal("0"), current_balance=Decimal("0"),
        )
        self.payment = sales_services.record_payment(
            business=self.business, customer=self.customer, amount=Decimal("100"), method="cash"
        )
        self.client.force_authenticate(user=self.user)

    def test_edit_without_a_version_still_works(self):
        """Existing clients keep working — the guard is opt-in."""
        response = self.client.patch(
            f"/api/payments/{self.payment.id}/", {"amount": "150"}, format="json"
        )
        self.assertEqual(response.status_code, 200)

    def test_edit_with_the_current_version_succeeds(self):
        response = self.client.patch(
            f"/api/payments/{self.payment.id}/",
            {"amount": "150", "expected_updated_at": self.payment.updated_at.isoformat()},
            format="json",
        )
        self.assertEqual(response.status_code, 200)

    def test_a_stale_edit_is_refused_instead_of_overwriting(self):
        """Two devices editing the same entry must not silently clobber."""
        stale_version = self.payment.updated_at

        # Device A saves first.
        self.client.patch(f"/api/payments/{self.payment.id}/", {"amount": "500"}, format="json")

        # Device B still holds the pre-edit version and tries to save 300.
        response = self.client.patch(
            f"/api/payments/{self.payment.id}/",
            {"amount": "300", "expected_updated_at": stale_version.isoformat()},
            format="json",
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "ENTRY_MODIFIED")

        # Device A's correction survives.
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.amount, Decimal("500"))

    def test_an_edit_may_move_an_entry_to_a_future_date(self):
        tomorrow = timezone.localtime() + timedelta(days=1)
        response = self.client.patch(
            f"/api/payments/{self.payment.id}/",
            {"amount": "150", "date": tomorrow.isoformat()},
            format="json",
        )
        self.assertEqual(response.status_code, 200)

    def test_an_edit_with_an_absurd_year_is_still_refused(self):
        far_future = timezone.localtime() + timedelta(days=500)
        response = self.client.patch(
            f"/api/payments/{self.payment.id}/",
            {"amount": "150", "date": far_future.isoformat()},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_entry_reports_whether_a_document_was_already_sent(self):
        history = self.client.get(f"/api/customers/{self.customer.id}/history/").json()["data"]
        self.assertFalse(history[0]["document_sent"])

        DocumentDelivery.objects.create(
            business=self.business, customer=self.customer, doc_type="receipt",
            requested_format="image", to_phone="923000000000",
            related_entry=self.payment, status="accepted",
        )

        history = self.client.get(f"/api/customers/{self.customer.id}/history/").json()["data"]
        # The app warns before editing this: the customer is holding a document
        # that will no longer match the ledger.
        self.assertTrue(history[0]["document_sent"])

    def test_a_failed_delivery_does_not_count_as_sent(self):
        DocumentDelivery.objects.create(
            business=self.business, customer=self.customer, doc_type="receipt",
            requested_format="image", to_phone="923000000000",
            related_entry=self.payment, status="failed",
        )
        history = self.client.get(f"/api/customers/{self.customer.id}/history/").json()["data"]
        self.assertFalse(history[0]["document_sent"])


class FutureDatedLedgerTests(TestCase):
    """The core accounting rule for scheduled entries."""

    def setUp(self):
        self.user = User.objects.create_user(username="fd@x.com", email="fd@x.com", password="pw")
        self.business = Business.objects.create(owner=self.user, business_name="Hamza Traders")
        self.customer = Customer.objects.create(
            business=self.business, name="Ali", phone="923000000000",
            opening_balance=Decimal("0"), current_balance=Decimal("0"),
        )
        self.today = business_today()

    def _sale(self, amount, on_date):
        return sales_services.record_sale(
            business=self.business, customer=self.customer,
            items=[{"item_name": "Rice", "quantity": Decimal("1"), "rate": amount}],
            date=to_entry_timestamp(on_date),
        )

    def test_a_future_sale_is_not_owed_today(self):
        self._sale(Decimal("1000"), self.today)
        self._sale(Decimal("5000"), self.today + timedelta(days=15))

        self.customer.refresh_from_db()
        # The whole point: the dashboard must not show 6000 as owed now.
        self.assertEqual(self.customer.current_balance, Decimal("1000"))
        self.assertEqual(self.customer.projected_balance, Decimal("6000"))

    def test_a_future_entry_still_gets_a_projected_balance_of_its_own(self):
        self._sale(Decimal("1000"), self.today)
        future_sale, _ = self._sale(Decimal("5000"), self.today + timedelta(days=15))
        future_sale.refresh_from_db()
        # So a planned invoice can print a sensible closing figure.
        self.assertEqual(future_sale.balance_after, Decimal("6000"))

    def test_backdated_and_future_entries_coexist_correctly(self):
        self._sale(Decimal("100"), self.today - timedelta(days=5))
        self._sale(Decimal("200"), self.today)
        self._sale(Decimal("400"), self.today + timedelta(days=10))

        self.customer.refresh_from_db()
        self.assertEqual(self.customer.current_balance, Decimal("300"))
        self.assertEqual(self.customer.projected_balance, Decimal("700"))

    def test_with_nothing_scheduled_both_balances_agree(self):
        self._sale(Decimal("750"), self.today - timedelta(days=1))
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.current_balance, self.customer.projected_balance)

    def test_a_matured_entry_becomes_owed_when_its_date_arrives(self):
        """Without the daily job a scheduled bill would never take effect.

        `current_balance` is only written on save, and nothing saves on the day
        a future entry matures.
        """
        from django.core.management import call_command

        future_sale, _ = self._sale(Decimal("5000"), self.today + timedelta(days=3))
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.current_balance, Decimal("0"))

        # Time passes: the entry is now dated in the past.
        ActivityEntry.objects.filter(pk=future_sale.pk).update(
            timestamp=to_entry_timestamp(self.today - timedelta(days=1))
        )
        call_command("apply_matured_entries")

        self.customer.refresh_from_db()
        self.assertEqual(self.customer.current_balance, Decimal("5000"))

    def test_is_future_helper(self):
        self.assertTrue(is_future(self.today + timedelta(days=1)))
        self.assertFalse(is_future(self.today))
        self.assertFalse(is_future(self.today - timedelta(days=1)))
