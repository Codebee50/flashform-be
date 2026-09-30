"""Background work that runs with or without a Celery worker.

Always queue tasks with `submit(task, kwargs)`, never `.delay()` / `.apply_async()`:

- USE_CELERY on (the default; docker-compose runs a worker): the task goes to Celery.
- USE_CELERY off (e.g. one Railway service and no worker): the task runs in a thread pool
  inside the web process, with the retry policy the task declares (`autoretry_for`,
  `max_retries`, `retry_backoff`, `retry_backoff_max`, `retry_jitter`).
- USE_CELERY on but the task can't be queued (broker down): it runs in a thread too, so
  it isn't dropped.

Threads are best effort: work still waiting (a `countdown`, or an email between retries)
is lost if the process stops, whereas Celery keeps it in the broker. Tasks take plain
JSON-able keyword arguments either way.
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial

from celery.utils.time import get_exponential_backoff_interval
from django.conf import settings
from django.db import connections

logger = logging.getLogger(__name__)

# Plenty for a few classes' emails and teacher events; anything beyond waits its turn.
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="background")


def submit(task, kwargs: dict, *, countdown: float = 0) -> None:
    """Run `task(**kwargs)` in the background, `countdown` seconds from now. Never raises."""
    if settings.USE_CELERY:
        try:
            task.apply_async(kwargs=kwargs, countdown=countdown or None)
            return
        except Exception:
            logger.exception("Could not queue %s on Celery; running it in this process", task.name)
    _spawn(partial(run_with_retries, task, kwargs), countdown)


def _spawn(fn, delay: float) -> None:
    """Run `fn` on the thread pool after `delay` seconds."""

    def job():
        try:
            fn()
        finally:
            # Pool threads outlive the job; don't leave their DB connections open.
            connections.close_all()

    if delay:
        timer = threading.Timer(delay, _executor.submit, args=[job])
        timer.daemon = True  # a pending delayed task must not block shutdown
        timer.start()
    else:
        _executor.submit(job)


def _retry_delay(task, retries: int) -> float:
    # Tasks only carry these attributes when they set them; the defaults are Celery's.
    backoff = getattr(task, "retry_backoff", False)
    if backoff:
        return get_exponential_backoff_interval(
            factor=int(max(1.0, backoff)),
            retries=retries,
            maximum=getattr(task, "retry_backoff_max", 600),
            full_jitter=getattr(task, "retry_jitter", True),
        )
    return task.default_retry_delay


def run_with_retries(task, kwargs: dict):
    """Run the task's body here, retrying the way a Celery worker would. Never raises.

    Outside a worker, a task's autoretry wrapper re-raises the original error instead of
    scheduling a retry, so the retry loop lives here. `task.request.retries` is set as on a
    worker, so `on_failure` handlers can report it.
    """
    retryable = tuple(getattr(task, "autoretry_for", ()))
    retries = 0
    while True:
        task.push_request(retries=retries)
        try:
            return task.run(**kwargs)
        except retryable as exc:
            if task.max_retries is not None and retries >= task.max_retries:
                logger.error("Task %s failed after %s retries: %s", task.name, retries, exc)
                task.on_failure(exc, None, (), kwargs, None)
                return None
            delay = _retry_delay(task, retries)
        except Exception as exc:
            logger.exception("Task %s failed", task.name)
            task.on_failure(exc, None, (), kwargs, None)
            return None
        finally:
            task.pop_request()
        time.sleep(delay)
        retries += 1
