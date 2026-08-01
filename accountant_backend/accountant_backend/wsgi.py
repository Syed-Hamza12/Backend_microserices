"""
WSGI config for accountant_backend project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/6.0/howto/deployment/wsgi/
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'accountant_backend.settings')

application = get_wsgi_application()

# Started here rather than in AppConfig.ready(): ready() also runs for every
# management command, so migrate/collectstatic/test would each spin up a worker
# that competes for jobs. wsgi.py is loaded only by the process actually
# serving requests. No-op unless RUN_WORKER_IN_PROCESS is set.
from apps.jobs.inprocess import start_if_enabled  # noqa: E402

start_if_enabled()
