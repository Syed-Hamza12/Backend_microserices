"""Prunes finished background jobs, expired undo tokens and orphaned uploads.

Run daily (cron / Windows Task Scheduler):

    python manage.py cleanup_artifacts

Nothing here touches business data. Sales, payments, customers, chat history and
delivery audit rows are permanent records and are never pruned — only the
by-products of processing them.
"""

from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.image_info_extractor.models import ExtractionJob
from apps.jobs.models import JobTask
from apps.sales.models import PendingUndo

# Finished jobs are only useful while someone might still poll them.
JOB_RETENTION_DAYS = 7

# Uploaded chat images are consumed by the extraction job and never shown again
# — the app renders the local file it just picked, not the server's copy — so
# once processing has had time to finish, the file is dead weight.
UPLOAD_RETENTION_DAYS = 7


class Command(BaseCommand):
    help = "Deletes finished job rows, expired undo tokens and orphaned uploaded images."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report without deleting.")
        parser.add_argument("--job-days", type=int, default=JOB_RETENTION_DAYS)
        parser.add_argument("--upload-days", type=int, default=UPLOAD_RETENTION_DAYS)

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        now = timezone.now()
        job_cutoff = now - timedelta(days=options["job_days"])
        upload_cutoff = now - timedelta(days=options["upload_days"])

        # --- finished jobs ---
        finished = JobTask.objects.filter(status__in=("done", "failed"), created_at__lt=job_cutoff)
        job_count = finished.count()

        # --- expired undo tokens ---
        # One-shot revert tokens with a 5-minute window; anything older can
        # never be used again.
        expired_undos = PendingUndo.objects.filter(expires_at__lt=now - timedelta(days=1))
        undo_count = expired_undos.count()

        # --- orphaned upload files ---
        # A file is orphaned when nothing in the database still points at it.
        # Checked against the database rather than by age alone, so an upload
        # still referenced by a queued job is never removed out from under it.
        uploads_dir = Path(settings.MEDIA_ROOT) / "uploads"
        referenced = set()
        for url in ExtractionJob.objects.values_list("source_image_url", flat=True):
            if url:
                referenced.add(Path(url).name)

        orphans = []
        orphan_bytes = 0
        if uploads_dir.is_dir():
            for candidate in uploads_dir.iterdir():
                if not candidate.is_file() or candidate.name in referenced:
                    continue
                modified = timezone.datetime.fromtimestamp(
                    candidate.stat().st_mtime, tz=timezone.get_current_timezone()
                )
                if modified < upload_cutoff:
                    orphans.append(candidate)
                    orphan_bytes += candidate.stat().st_size

        if dry_run:
            self.stdout.write(f"would delete {job_count} finished job(s) older than {options['job_days']}d")
            self.stdout.write(f"would delete {undo_count} expired undo token(s)")
            self.stdout.write(
                f"would delete {len(orphans)} orphaned upload(s) ({orphan_bytes / 1024:.0f} KB)"
            )
            self.stdout.write(self.style.WARNING("Dry run — nothing was deleted."))
            return

        # ExtractionJob has a FK to JobTask and is removed by the cascade.
        finished.delete()
        expired_undos.delete()

        deleted_files = 0
        for candidate in orphans:
            try:
                candidate.unlink()
                deleted_files += 1
            except OSError as exc:
                self.stderr.write(f"could not delete {candidate.name}: {exc}")

        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted {job_count} job(s), {undo_count} undo token(s), "
                f"{deleted_files} upload(s) ({orphan_bytes / 1024:.0f} KB reclaimed)."
            )
        )
