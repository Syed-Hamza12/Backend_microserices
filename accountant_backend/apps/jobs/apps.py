import logging
import os
import sys
import threading
import time

from django.apps import AppConfig

logger = logging.getLogger(__name__)


class JobsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.jobs'
    label = 'jobs'

    def ready(self):
        # Runs `manage.py runworker`'s own poll loop inline, as a background
        # thread of the dev server process, so `runserver` alone is enough to
        # process voice/image/document-send jobs — a separate `runworker`
        # process is easy to forget to (re)start, and unlike `runserver` it
        # has no autoreload at all, so a forgotten restart after a code
        # change silently keeps serving stale job-handling code (a real bug
        # this caused: a WhatsApp-send fix landed, `runserver` picked it up
        # immediately, but the still-running old `runworker` process kept
        # skipping it for every voice message until someone noticed).
        #
        # Dev-server only, and exactly once even with the autoreloader (which
        # re-execs this process — RUN_MAIN is Django's own marker for "this
        # is the actual serving child, not the reloader's launcher"). Never
        # runs for `runworker`/`migrate`/`test`/`shell`/a real WSGI/ASGI
        # deployment — none of those have "runserver" in argv, and running a
        # background job loop inside *this* process is a dev convenience,
        # not something to also do under a real multi-worker production
        # server, where a standalone `runworker` process (or several,
        # scaled independently) remains the right shape.
        is_runserver = "runserver" in sys.argv
        autoreload_child = os.environ.get("RUN_MAIN") == "true"
        noreload = "--noreload" in sys.argv
        if is_runserver and (autoreload_child or noreload):
            thread = threading.Thread(target=self._run_worker_loop, daemon=True, name="jobs-worker-inline")
            thread.start()

    @staticmethod
    def _run_worker_loop():
        from .management.commands.runworker import process_next_job

        logger.info("jobs worker running inline alongside runserver")
        while True:
            try:
                processed = process_next_job()
            except Exception:  # noqa: BLE001 - this loop must never die silently
                logger.exception("inline jobs worker: unexpected error, continuing")
                processed = False
            if not processed:
                time.sleep(2.0)
