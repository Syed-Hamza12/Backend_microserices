"""Runs the job worker inside the web process, as a background thread.

For hosts where a separate always-on process costs money (Render's free tier
has no free Background Worker; PythonAnywhere's free tier has no always-on
task). Without this, `runworker` simply never runs in production and every
photo upload and WhatsApp send sits at "Queued" forever with no error — the
failure the developer guide already lists as the most commonly forgotten one.

This is the same loop `manage.py runworker` runs, sharing `process_next_job`,
so there is one implementation of "what a job does" and not two. Running the
dedicated command remains the better option wherever it is affordable; the two
are safe to run at the same time because `_claim_next_job` claims jobs with a
conditional UPDATE, so only one worker can ever own a given job.

Enable with `RUN_WORKER_IN_PROCESS=True`.
"""

import logging
import threading
import time

from django.conf import settings
from django.db import connection

logger = logging.getLogger(__name__)

_started = threading.Lock()
_is_running = False


def _loop(poll_interval):
    # Imported here, not at module scope: this module is imported from wsgi.py
    # during startup, and pulling in the models any earlier trips
    # AppRegistryNotReady.
    from apps.jobs.management.commands.runworker import process_next_job

    while True:
        try:
            processed = process_next_job()
        except Exception:  # noqa: BLE001 - one bad job must never end the loop
            # process_next_job already marks the job failed for handler errors;
            # reaching here means something outside that (e.g. the database
            # went away), which a web process must survive.
            logger.exception("in-process worker iteration failed")
            processed = False
        finally:
            # Long-lived threads hold their connection open forever otherwise,
            # which is what exhausts a free Postgres tier's small connection
            # limit and, on SQLite, keeps a file handle on a database the web
            # requests also need.
            connection.close()

        if not processed:
            time.sleep(poll_interval)


def start_if_enabled():
    """Starts the worker thread once per process. Safe to call more than once."""
    global _is_running

    if not getattr(settings, "RUN_WORKER_IN_PROCESS", False):
        return False

    with _started:
        if _is_running:
            return False
        _is_running = True

    thread = threading.Thread(
        target=_loop,
        args=(getattr(settings, "WORKER_POLL_INTERVAL", 2.0),),
        # Daemon so it never blocks the web server from shutting down. A job
        # interrupted mid-flight is left claimed; see the stale-job note in
        # `_claim_next_job`.
        daemon=True,
        name="jobs-inprocess-worker",
    )
    thread.start()
    logger.info("in-process job worker started")
    return True
