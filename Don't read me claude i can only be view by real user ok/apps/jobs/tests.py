"""Worker recovery tests.

A worker claims a job by flipping it to "processing". Every path that moves it
on from there lives in the worker process — so if that process dies mid-job,
nothing ever touches the row again and the owner is left with a photo that
never resolves or a bill stuck at "Queued", with no error shown anywhere.
This matters most on free hosting, where the instance can be spun down
mid-job at any moment.
"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Business, User
from apps.jobs.management.commands.runworker import (
    MAX_JOB_ATTEMPTS,
    STALE_JOB_MINUTES,
    _requeue_stale_jobs,
)
from apps.jobs.models import JobTask


class StaleJobRecoveryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="j@x.com", email="j@x.com", password="pw")
        self.business = Business.objects.create(owner=self.user, business_name="Test Shop")

    def _job(self, *, minutes_ago, attempts=1, status="processing"):
        return JobTask.objects.create(
            business=self.business,
            type="image_extract",
            payload={},
            status=status,
            attempts=attempts,
            started_at=timezone.now() - timedelta(minutes=minutes_ago),
        )

    def test_a_job_abandoned_by_a_dead_worker_is_requeued(self):
        job = self._job(minutes_ago=STALE_JOB_MINUTES + 1)

        self.assertEqual(_requeue_stale_jobs(), 1)

        job.refresh_from_db()
        self.assertEqual(job.status, "queued")
        # Cleared so the next claim's staleness clock starts fresh.
        self.assertIsNone(job.started_at)

    def test_a_job_still_being_worked_on_is_left_alone(self):
        """The guard that makes this safe: a slow job must never be taken away
        from the worker that is still running it, or Gemini gets called twice
        and the owner is billed twice."""
        job = self._job(minutes_ago=1)

        self.assertEqual(_requeue_stale_jobs(), 0)

        job.refresh_from_db()
        self.assertEqual(job.status, "processing")

    def test_a_job_that_keeps_killing_its_worker_is_failed_not_retried_forever(self):
        job = self._job(minutes_ago=STALE_JOB_MINUTES + 1, attempts=MAX_JOB_ATTEMPTS)

        _requeue_stale_jobs()

        job.refresh_from_db()
        self.assertEqual(job.status, "failed")
        self.assertIn("Abandoned", job.error)
        self.assertIsNotNone(job.finished_at)

    def test_finished_jobs_are_never_touched(self):
        for status in ("done", "failed", "queued"):
            with self.subTest(status=status):
                job = self._job(minutes_ago=STALE_JOB_MINUTES + 1, status=status)
                _requeue_stale_jobs()
                job.refresh_from_db()
                self.assertEqual(job.status, status)
