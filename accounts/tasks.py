import logging
import smtplib

import requests
from celery import Task, shared_task

from .email_service import TRANSPORTS, TransientEmailError

logger = logging.getLogger(__name__)

# Network failures and provider-side (5xx/429) errors. Anything else, e.g. a Brevo 4xx
# or a rejected SMTP recipient, will not succeed on retry.
RETRYABLE_ERRORS = (
    requests.RequestException,
    TransientEmailError,
    ConnectionError,
    TimeoutError,
    smtplib.SMTPConnectError,
    smtplib.SMTPServerDisconnected,
)


class EmailTask(Task):
    def on_failure(self, exc, task_id, args, kwargs, einfo):
        logger.error(
            "Giving up on email to %s (subject %r) after %s retries: %s",
            kwargs.get("email"),
            kwargs.get("subject"),
            self.request.retries,
            exc,
        )


@shared_task(
    base=EmailTask,
    name="accounts.tasks.send_email_task",
    autoretry_for=RETRYABLE_ERRORS,
    retry_backoff=True,  # 1s, 2s, 4s, ... with jitter
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=5,
)
def send_email_task(
    email: str, subject: str, html: str, text: str | None = None, transport: str = "brevo"
) -> bool:
    """Send one email. Takes plain data only, never model instances.

    Not strictly idempotent: if the worker dies after the provider accepted the email but
    before the task is acked, the redelivered task sends it again. A rare duplicate
    email is acceptable here.
    """
    return TRANSPORTS[transport](email, subject, html, text)
