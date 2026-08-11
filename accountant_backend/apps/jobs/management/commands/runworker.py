import logging
import time
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import F
from django.utils import timezone

from apps.jobs.models import JobTask

logger = logging.getLogger(__name__)


def _handler_for(job_type):
    if job_type == "document_send":
        from apps.documents.delivery import handle_document_send_job

        return handle_document_send_job
    if job_type == "image_extract":
        from apps.image_info_extractor.services import handle_image_extract_job

        return handle_image_extract_job
    if job_type == "voice_transcribe":
        from apps.voice_transcriber.services import handle_voice_transcribe_job

        return handle_voice_transcribe_job
    raise ValueError(f"No worker handler registered for job type: {job_type}")


#: How long a job may sit in "processing" before it is treated as abandoned.
#: Comfortably longer than the slowest real job (a Gemini call plus a paced
#: WhatsApp send), so a slow job is never stolen from a live worker.
STALE_JOB_MINUTES = 15

#: A job that keeps dying mid-run must not be requeued forever — after this
#: many attempts it is failed so someone can look at it.
MAX_JOB_ATTEMPTS = 3


def _requeue_stale_jobs():
    """Returns jobs whose worker died mid-run to the queue.

    A worker claims a job by setting it to "processing". If that process then
    dies — a free host spinning the instance down mid-job, a deploy, a machine
    losing power — nothing ever moves the row again: the claim query only looks
    at "queued". The owner sees a photo that never resolves or a bill stuck at
    "Queued", with no error anywhere.

    Only jobs older than [STALE_JOB_MINUTES] are touched, so this can never
    take a job away from a worker that is still working on it.
    """
    cutoff = timezone.now() - timedelta(minutes=STALE_JOB_MINUTES)
    stale = JobTask.objects.filter(status="processing", started_at__lt=cutoff)

    # Give up on anything that has already been retried too often, rather than
    # letting a job that reliably kills its worker cycle forever.
    exhausted = list(stale.filter(attempts__gte=MAX_JOB_ATTEMPTS).values_list("id", flat=True))
    if exhausted:
        JobTask.objects.filter(id__in=exhausted).update(
            status="failed",
            error=f"Abandoned after {MAX_JOB_ATTEMPTS} attempts (worker died mid-job).",
            finished_at=timezone.now(),
        )
        logger.warning("failed %s job(s) that exceeded the retry limit", len(exhausted))

    requeued = stale.filter(attempts__lt=MAX_JOB_ATTEMPTS).update(status="queued", started_at=None)
    if requeued:
        logger.info("requeued %s stale job(s)", requeued)
    return requeued


def _claim_next_job():
    """Atomically takes ownership of the oldest queued job, or returns None.

    Selecting the job and then saving it as 'processing' in two steps let two
    workers (or one worker and a restarted copy of itself) claim the same job:
    both would call Gemini, both would bill the business's quota, and both
    would post a reply into the same conversation. The conditional UPDATE means
    only the worker whose write actually changed a row proceeds.
    """
    while True:
        job = JobTask.objects.filter(status="queued").order_by("created_at").first()
        if job is None:
            return None

        claimed = JobTask.objects.filter(pk=job.pk, status="queued").update(
            status="processing", started_at=timezone.now(), attempts=F("attempts") + 1
        )
        if claimed:
            job.refresh_from_db()
            return job
        # Another worker got there first — look for the next one.


def process_next_job():
    job = _claim_next_job()
    if job is None:
        # Nothing queued — the moment to check whether an earlier worker died
        # holding a job. Only on the idle path, so a busy worker never pays for
        # this query.
        if _requeue_stale_jobs():
            job = _claim_next_job()
    if job is None:
        return False

    try:
        handler = _handler_for(job.type)
        result = handler(job)
        job.result = result
        job.status = "done"
    except Exception as exc:  # noqa: BLE001 - worker must never crash the loop on a bad job
        job.status = "failed"
        job.error = str(exc)
    finally:
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "result", "error", "finished_at"])

    return True


class Command(BaseCommand):
    help = "Polls the oldest queued JobTask and processes it (PDF generation, image extraction, ...)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Process at most one job, then exit.")
        parser.add_argument("--poll-interval", type=float, default=2.0)

    def handle(self, *args, **options):
        poll_interval = options["poll_interval"]
        if options["once"]:
            processed = process_next_job()
            self.stdout.write(self.style.SUCCESS(f"Processed a job: {processed}"))
            return

        self.stdout.write(self.style.SUCCESS("Worker loop started. Ctrl+C to stop."))
        while True:
            processed = process_next_job()
            if not processed:
                time.sleep(poll_interval)
