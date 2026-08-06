"""Pagination, media serving and artifact cleanup.

These are the production-readiness behaviours: bounded responses, media that
works with DEBUG off, and by-products that don't accumulate forever.
"""

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.accounts.models import Business, User
from apps.customers.models import Customer
from apps.image_info_extractor.models import ExtractionJob
from apps.jobs.models import JobTask
from apps.notifications.models import Notification
from apps.sales import services as sales_services
from apps.sales.models import PendingUndo


class PaginationTestCase(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="p@x.com", email="p@x.com", password="pw")
        self.business = Business.objects.create(owner=self.user, business_name="Hamza Traders")
        self.client.force_authenticate(user=self.user)


class CustomerPaginationTests(PaginationTestCase):
    def setUp(self):
        super().setUp()
        for i in range(25):
            Customer.objects.create(
                business=self.business, name=f"Customer {i:03d}", phone=f"92300000{i:04d}",
                opening_balance=Decimal("0"), current_balance=Decimal("0"),
            )

    def test_data_is_still_a_plain_array(self):
        """Existing clients must not break the day pagination ships."""
        body = self.client.get("/api/customers/").json()
        self.assertIsInstance(body["data"], list)
        self.assertTrue(body["success"])

    def test_page_metadata_describes_the_slice(self):
        body = self.client.get("/api/customers/", {"limit": 10}).json()
        self.assertEqual(len(body["data"]), 10)
        self.assertEqual(body["page"]["total"], 25)
        self.assertEqual(body["page"]["limit"], 10)
        self.assertTrue(body["page"]["has_more"])

    def test_paging_through_returns_everything_exactly_once(self):
        seen = []
        offset = 0
        for _ in range(10):
            body = self.client.get("/api/customers/", {"limit": 10, "offset": offset}).json()
            seen.extend(c["id"] for c in body["data"])
            if not body["page"]["has_more"]:
                break
            offset += len(body["data"])

        self.assertEqual(len(seen), 25)
        self.assertEqual(len(set(seen)), 25, "a row was returned on two different pages")

    def test_last_page_reports_no_more(self):
        body = self.client.get("/api/customers/", {"limit": 10, "offset": 20}).json()
        self.assertEqual(len(body["data"]), 5)
        self.assertFalse(body["page"]["has_more"])

    def test_limit_is_capped(self):
        body = self.client.get("/api/customers/", {"limit": 99999}).json()
        self.assertLessEqual(body["page"]["limit"], 500)

    def test_nonsense_paging_parameters_fall_back_to_defaults(self):
        body = self.client.get("/api/customers/", {"limit": "abc", "offset": "-5"}).json()
        self.assertEqual(body["page"]["offset"], 0)
        self.assertGreater(body["page"]["limit"], 0)


class HistoryPaginationTests(PaginationTestCase):
    def setUp(self):
        super().setUp()
        self.customer = Customer.objects.create(
            business=self.business, name="Ali", phone="923000000000",
            opening_balance=Decimal("0"), current_balance=Decimal("0"),
        )
        for _ in range(12):
            sales_services.record_payment(
                business=self.business, customer=self.customer,
                amount=Decimal("100"), method="cash",
            )

    def test_history_is_paginated_and_complete_across_pages(self):
        first = self.client.get(
            f"/api/customers/{self.customer.id}/history/", {"limit": 5}
        ).json()
        self.assertEqual(len(first["data"]), 5)
        self.assertEqual(first["page"]["total"], 12)

        collected = []
        offset = 0
        while True:
            body = self.client.get(
                f"/api/customers/{self.customer.id}/history/", {"limit": 5, "offset": offset}
            ).json()
            collected.extend(body["data"])
            if not body["page"]["has_more"]:
                break
            offset += len(body["data"])

        # Balances are computed client-side from the full ledger, so a paged
        # fetch losing or repeating a row would show as wrong money.
        self.assertEqual(len(collected), 12)
        self.assertEqual(len({e["id"] for e in collected}), 12)

    def test_ordering_is_stable_across_pages(self):
        page_one = self.client.get(
            f"/api/customers/{self.customer.id}/history/", {"limit": 6, "offset": 0}
        ).json()["data"]
        page_two = self.client.get(
            f"/api/customers/{self.customer.id}/history/", {"limit": 6, "offset": 6}
        ).json()["data"]

        ids = [e["id"] for e in page_one + page_two]
        self.assertEqual(ids, sorted(ids), "pages must follow one deterministic order")


class NotificationPaginationTests(PaginationTestCase):
    def test_notifications_are_paginated(self):
        for i in range(15):
            Notification.objects.create(business=self.business, type="payment_received", payload={"i": i})

        body = self.client.get("/api/notifications/", {"limit": 5}).json()
        self.assertEqual(len(body["data"]), 5)
        self.assertEqual(body["page"]["total"], 15)


class LogoServingTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="l@x.com", email="l@x.com", password="pw")
        self.business = Business.objects.create(owner=self.user, business_name="Hamza Traders")
        self.client.force_authenticate(user=self.user)

    def test_no_logo_returns_a_clean_404(self):
        response = self.client.get("/api/business/logo/file/")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "NO_LOGO")

    def test_profile_reports_whether_a_logo_exists(self):
        body = self.client.get("/api/business/profile/").json()["data"]
        self.assertFalse(body["has_logo"])

        self.business.logo_url = "http://testserver/media/business_logos/x.png"
        self.business.save(update_fields=["logo_url"])

        body = self.client.get("/api/business/profile/").json()["data"]
        self.assertTrue(body["has_logo"])

    def test_a_missing_file_does_not_500(self):
        self.business.logo_url = "http://testserver/media/business_logos/gone.png"
        self.business.save(update_fields=["logo_url"])
        response = self.client.get("/api/business/logo/file/")
        self.assertEqual(response.status_code, 404)

    def test_path_traversal_in_a_stored_url_is_refused(self):
        # Defence in depth: the value is server-generated today, but joining an
        # unchecked relative path onto MEDIA_ROOT is how that becomes an
        # arbitrary-file read if it ever stops being.
        self.business.logo_url = "http://testserver/media/../../../../etc/passwd"
        self.business.save(update_fields=["logo_url"])
        response = self.client.get("/api/business/logo/file/")
        self.assertEqual(response.status_code, 404)

    def test_serving_is_scoped_to_the_caller(self):
        other_user = User.objects.create_user(username="o@x.com", email="o@x.com", password="pw")
        Business.objects.create(
            owner=other_user, business_name="Other Shop",
            logo_url="http://testserver/media/business_logos/theirs.png",
        )
        # The caller has no logo of their own; another business having one must
        # not leak through this endpoint.
        self.assertEqual(self.client.get("/api/business/logo/file/").status_code, 404)


class CleanupArtifactsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="c@x.com", email="c@x.com", password="pw")
        self.business = Business.objects.create(owner=self.user, business_name="Hamza Traders")
        self.customer = Customer.objects.create(
            business=self.business, name="Ali", phone="923000000000",
            opening_balance=Decimal("0"), current_balance=Decimal("0"),
        )

    def _job(self, status_value, age_days):
        job = JobTask.objects.create(
            business=self.business, type="document_send", payload={}, status=status_value
        )
        JobTask.objects.filter(pk=job.pk).update(
            created_at=timezone.now() - timedelta(days=age_days)
        )
        return job

    def test_old_finished_jobs_are_removed(self):
        old_done = self._job("done", 30)
        old_failed = self._job("failed", 30)
        recent = self._job("done", 1)

        call_command("cleanup_artifacts")

        self.assertFalse(JobTask.objects.filter(pk=old_done.pk).exists())
        self.assertFalse(JobTask.objects.filter(pk=old_failed.pk).exists())
        self.assertTrue(JobTask.objects.filter(pk=recent.pk).exists())

    def test_queued_and_processing_jobs_are_never_removed(self):
        """Age is not a reason to delete work that hasn't run."""
        queued = self._job("queued", 90)
        processing = self._job("processing", 90)

        call_command("cleanup_artifacts")

        self.assertTrue(JobTask.objects.filter(pk=queued.pk).exists())
        self.assertTrue(JobTask.objects.filter(pk=processing.pk).exists())

    def test_expired_undo_tokens_are_removed_but_live_ones_survive(self):
        entry = sales_services.record_payment(
            business=self.business, customer=self.customer, amount=Decimal("100"), method="cash"
        )
        stale = PendingUndo.objects.create(
            business=self.business, entry_id=entry.id, action="edit", snapshot={},
            expires_at=timezone.now() - timedelta(days=5),
        )
        live = PendingUndo.objects.create(
            business=self.business, entry_id=entry.id, action="edit", snapshot={},
            expires_at=timezone.now() + timedelta(minutes=5),
        )

        call_command("cleanup_artifacts")

        self.assertFalse(PendingUndo.objects.filter(pk=stale.pk).exists())
        self.assertTrue(PendingUndo.objects.filter(pk=live.pk).exists())

    def test_business_data_is_never_touched(self):
        sale, _ = sales_services.record_sale(
            business=self.business, customer=self.customer,
            items=[{"item_name": "Rice", "quantity": Decimal("1"), "rate": Decimal("500")}],
        )
        self._job("done", 60)

        call_command("cleanup_artifacts")

        self.assertTrue(Customer.objects.filter(pk=self.customer.pk).exists())
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.current_balance, Decimal("500"))
        from apps.sales.models import ActivityEntry
        self.assertTrue(ActivityEntry.objects.filter(pk=sale.pk).exists())

    def test_dry_run_deletes_nothing(self):
        job = self._job("done", 60)
        call_command("cleanup_artifacts", "--dry-run")
        self.assertTrue(JobTask.objects.filter(pk=job.pk).exists())


class OrphanedUploadCleanupTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u@x.com", email="u@x.com", password="pw")
        self.business = Business.objects.create(owner=self.user, business_name="Hamza Traders")

    def _write_upload(self, name, age_days):
        uploads = Path(settings.MEDIA_ROOT) / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        path = uploads / name
        path.write_bytes(b"fake image bytes")
        import os
        old = (timezone.now() - timedelta(days=age_days)).timestamp()
        os.utime(path, (old, old))
        return path

    def test_an_upload_still_referenced_is_kept(self):
        """Age alone must not delete a file a queued job still needs."""
        with self.settings(MEDIA_ROOT=self._temp_media()):
            referenced = self._write_upload("referenced.jpg", 60)
            job = JobTask.objects.create(business=self.business, type="image_extract", payload={})
            ExtractionJob.objects.create(
                business=self.business, job_task=job,
                source_image_url="/media/uploads/referenced.jpg", status="pending",
            )

            call_command("cleanup_artifacts")

            self.assertTrue(referenced.exists())

    def test_an_old_unreferenced_upload_is_removed(self):
        with self.settings(MEDIA_ROOT=self._temp_media()):
            orphan = self._write_upload("orphan.jpg", 60)
            recent = self._write_upload("recent.jpg", 1)

            call_command("cleanup_artifacts")

            self.assertFalse(orphan.exists())
            # Recent files are left alone: a job may still be about to run.
            self.assertTrue(recent.exists())

    def _temp_media(self):
        import tempfile
        directory = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        return directory
