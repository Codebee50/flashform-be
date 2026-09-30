"""Transactional email (PRD §8 "Email sending").

Emails go through Brevo's HTTP API. `EmailService(email)` queues each send
(`accounts.tasks.send_email_task`, via `flashform.background.submit`: on Celery, or in a
thread with USE_CELERY off) once the surrounding transaction commits, so the HTTP response
never waits on Brevo and nothing is sent for a rolled-back write. The task retries network
errors and Brevo 5xx/429; other 4xx responses are logged and dropped. Pass
`background=False` to send inline (management commands, the shell).

With `BREVO_API_KEY` empty (local dev), emails are logged instead of sent, so they
show up in `docker compose logs worker` (or the backend's logs with USE_CELERY off).

Sending never raises to the caller: failures are logged.
"""

import html as html_lib
import logging
import re
from functools import partial

import requests
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.template.loader import render_to_string
from django.utils.html import strip_tags

from flashform import background

logger = logging.getLogger(__name__)

BREVO_URL = "https://api.brevo.com/v3/smtp/email"
BREVO_TIMEOUT = 10  # seconds


class TransientEmailError(Exception):
    """The provider answered 5xx or 429: worth retrying later."""


def html_to_text(html: str) -> str:
    """Plain-text version of an HTML email: tags stripped, entities decoded, blank runs collapsed."""
    body = re.sub(r"(?is)<(head|style|script)\b.*?</\1>", "", html)
    text = html_lib.unescape(strip_tags(body))
    lines = [line.strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def deliver_brevo_mail(email: str, subject: str, html: str, text: str | None = None) -> bool:
    """Send one email through Brevo.

    Returns True when Brevo accepted it (or it was logged because no API key is set) and
    False when Brevo rejected it permanently (4xx other than 429), which is logged with the
    response body. Raises `requests.RequestException` or `TransientEmailError` for failures
    worth retrying; `send_email_task` retries those. Use `send_brevo_mail` to never raise.
    """
    if not settings.BREVO_API_KEY:
        logger.info(
            "BREVO_API_KEY is empty, so this email was logged instead of sent.\n"
            "To: %s\nSubject: %s\n\n%s",
            email,
            subject,
            text or html_to_text(html),
        )
        return True

    payload = {
        "sender": {"name": settings.APP_NAME, "email": settings.BREVO_FROM_EMAIL},
        "to": [{"email": email}],
        "subject": subject,
        "htmlContent": html,
    }
    if text:
        payload["textContent"] = text

    response = requests.post(
        BREVO_URL,
        headers={
            "api-key": settings.BREVO_API_KEY,
            "accept": "application/json",
            "content-type": "application/json",
        },
        json=payload,
        timeout=BREVO_TIMEOUT,
    )

    if response.status_code == 429 or response.status_code >= 500:
        raise TransientEmailError(f"Brevo returned {response.status_code}: {response.text[:1000]}")
    if response.status_code >= 400:
        logger.error(
            "Brevo rejected email to %s (subject %r) with %s: %s",
            email,
            subject,
            response.status_code,
            response.text[:1000],
        )
        return False
    return True


def send_brevo_mail(email: str, subject: str, html: str, text: str | None = None) -> bool:
    """Send one email through Brevo, inline. Never raises; returns whether it was accepted."""
    try:
        return deliver_brevo_mail(email, subject, html, text)
    except Exception:
        logger.exception("Failed to send email to %s (subject %r)", email, subject)
        return False


def deliver_smtp_mail(email: str, subject: str, html: str, text: str | None = None) -> bool:
    """Send one email with Django's mail backend (the EMAIL_* settings). Raises on failure."""
    message = EmailMultiAlternatives(
        subject=subject,
        body=text or html_to_text(html),
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[email],
    )
    message.attach_alternative(html, "text/html")
    message.send()
    return True


TRANSPORTS = {"brevo": deliver_brevo_mail, "smtp": deliver_smtp_mail}


def _enqueue(**kwargs) -> None:
    # Runs after commit, outside the view's error handling. `submit` never raises: with the
    # broker down (or USE_CELERY off) the email is sent from a thread of this process.
    from .tasks import send_email_task  # tasks imports this module

    background.submit(send_email_task, kwargs)


class EmailService:
    def __init__(self, email: str, background: bool = True):
        self.email = email
        self.background = background

    def _send(self, transport: str, subject: str, html: str, text: str | None) -> bool:
        if self.background:
            # Only plain data goes to the task. It is queued only if the current
            # transaction commits (immediately when there is none).
            transaction.on_commit(
                partial(
                    _enqueue,
                    email=self.email,
                    subject=subject,
                    html=html,
                    text=text,
                    transport=transport,
                )
            )
            return True
        try:
            return TRANSPORTS[transport](self.email, subject, html, text)
        except Exception:
            logger.exception("Failed to send email to %s (subject %r)", self.email, subject)
            return False

    def send_brevo_mail(self, subject: str, html: str, text: str | None = None) -> bool:
        return self._send("brevo", subject, html, text)

    def send_smtp_mail(self, subject: str, html: str, text: str | None = None) -> bool:
        return self._send("smtp", subject, html, text)

    def send_template_mail(self, subject: str, template_name: str, context: dict) -> bool:
        """Render `template_name` and send it with a plain-text alternative.

        Returns False if rendering failed. Otherwise returns True once queued (background)
        or whether Brevo accepted it (inline).
        """
        context = {
            **context,
            "app_name": settings.APP_NAME,
            "support_email": settings.SUPPORT_EMAIL,
            "frontend_url": settings.FRONTEND_URL,
        }
        try:
            html = render_to_string(template_name, context)
        except Exception:
            logger.exception("Error rendering email template %s", template_name)
            return False
        return self.send_brevo_mail(subject, html, html_to_text(html))

    def send_raw_mail(self, subject: str, body: str) -> bool:
        return self.send_brevo_mail(subject, body, html_to_text(body))
