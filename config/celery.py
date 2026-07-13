"""
Celery application configuration for the artikate-assessment project.

This module creates the Celery app instance and configures it to read settings
from Django's settings module under the "CELERY_" namespace prefix.

Usage:
    Start a worker:
        celery -A config worker -l info

    Start with concurrency and rate limit awareness:
        celery -A config worker -l info --concurrency=4

    Monitor via Flower (optional):
        celery -A config flower

How autodiscover_tasks works:
    Celery scans all INSTALLED_APPS for a `tasks.py` module and registers
    any @shared_task or @app.task decorators it finds. This means section02/tasks.py
    is auto-discovered and the send_transactional_email task is available without
    explicit imports.

The debug_task is a built-in sanity check — call it to verify the worker is
running and the request context is correctly populated:
    from config.celery import debug_task
    debug_task.delay()
"""

import os

from celery import Celery

# Set the default Django settings module for Celery processes.
# Without this, celery -A config worker would not know which settings to load.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

# Create the Celery application instance.
# The name ("config") is used as the default task name prefix.
app = Celery("config")

# Load Celery settings from Django's settings module.
# All keys prefixed with "CELERY_" in settings.py are mapped to Celery config.
# For example: CELERY_BROKER_URL → broker_url, CELERY_TASK_ACKS_LATE → task_acks_late
app.config_from_object("django.conf:settings", namespace="CELERY")

# Auto-discover tasks in all Django apps listed in INSTALLED_APPS.
# Celery looks for a tasks.py in each app directory.
app.autodiscover_tasks()


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    """
    Built-in diagnostic task for verifying Celery worker health.

    Prints the current task request context (routing info, task ID, retries,
    etc.) to the worker's stdout. Useful for confirming:
    - The worker is running and connected to the broker.
    - Django settings are correctly loaded in the worker process.
    - The task serializer and routing are working.

    Call from a Django shell or test to trigger:
        from config.celery import debug_task
        debug_task.delay()

    `ignore_result=True` means the result is not stored in the result backend,
    since this task is only for diagnostics.
    """
    print(f"Request: {self.request!r}")
