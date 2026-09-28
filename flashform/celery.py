"""
Celery app. Configured from Django settings (the `CELERY_*` names); tasks are
discovered from each installed app's `tasks.py`.

Run a worker with: celery -A flashform worker -l info
"""

import logging
import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "flashform.settings")

logger = logging.getLogger(__name__)

app = Celery("flashform")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


@app.task(name="flashform.ping")
def ping() -> str:
    """Trivial task to prove a worker is consuming the queue."""
    logger.info("pong")
    return "pong"
