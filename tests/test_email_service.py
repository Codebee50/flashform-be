import logging
from unittest import mock

import pytest
import requests
from django.core import mail
from django.db import transaction
from kombu.exceptions import OperationalError

from accounts.email_service import (
    BREVO_URL,
    EmailService,
    TransientEmailError,
    html_to_text,
    send_brevo_mail,
)
from accounts.tasks import send_email_task
from flashform.celery import app as celery_app

POST = "accounts.email_service.requests.post"
APPLY_ASYNC = "accounts.tasks.send_email_task.apply_async"


def brevo_response(status_code: int, body: str = '{"messageId": "<id@brevo>"}') -> mock.Mock:
    return mock.Mock(status_code=status_code, text=body)


@pytest.fixture
def brevo_key(settings):
    settings.BREVO_API_KEY = "test-brevo-key"
    settings.BREVO_FROM_EMAIL = "no-reply@flashform.test"
    settings.APP_NAME = "Flashform"


def task_kwargs(**overrides) -> dict:
    return {"email": "ada@example.com", "subject": "Hello", "html": "<p>Hi</p>", "text": "Hi", **overrides}


# --- Brevo request ----------------------------------------------------------


def test_brevo_request_shape(brevo_key):
    with mock.patch(POST, return_value=brevo_response(201)) as post:
        assert send_brevo_mail("ada@example.com", "Hello", "<p>Hi <b>Ada</b></p>", "Hi Ada") is True

    post.assert_called_once_with(
        BREVO_URL,
        headers={
            "api-key": "test-brevo-key",
            "accept": "application/json",
            "content-type": "application/json",
        },
        json={
            "sender": {"name": "Flashform", "email": "no-reply@flashform.test"},
            "to": [{"email": "ada@example.com"}],
            "subject": "Hello",
            "htmlContent": "<p>Hi <b>Ada</b></p>",
            "textContent": "Hi Ada",
        },
        timeout=10,
    )


def test_brevo_request_omits_text_content_when_none(brevo_key):
    with mock.patch(POST, return_value=brevo_response(201)) as post:
        send_brevo_mail("ada@example.com", "Hello", "<p>Hi</p>")

    assert "textContent" not in post.call_args.kwargs["json"]


def test_no_api_key_logs_the_email_instead_of_sending(settings, caplog):
    settings.BREVO_API_KEY = ""

    with mock.patch(POST) as post, caplog.at_level(logging.INFO, logger="accounts.email_service"):
        assert send_brevo_mail("ada@example.com", "Verify", "<p>ignored</p>", "Open http://x/y") is True

    post.assert_not_called()
    assert "To: ada@example.com" in caplog.text
    assert "Subject: Verify" in caplog.text
    assert "Open http://x/y" in caplog.text


# --- Failures ---------------------------------------------------------------


def test_failed_brevo_call_is_logged_and_does_not_raise(brevo_key, caplog):
    with mock.patch(POST, side_effect=requests.ConnectionError("boom")):
        assert send_brevo_mail("ada@example.com", "Hello", "<p>Hi</p>") is False

    assert "Failed to send email to ada@example.com" in caplog.text


def test_brevo_4xx_is_logged_with_body_and_not_raised(brevo_key, caplog):
    body = '{"code": "invalid_parameter", "message": "sender not valid"}'
    with mock.patch(POST, return_value=brevo_response(400, body)):
        assert send_brevo_mail("ada@example.com", "Hello", "<p>Hi</p>") is False

    assert "sender not valid" in caplog.text


def test_inline_service_send_does_not_raise(brevo_key):
    with mock.patch(POST, side_effect=requests.Timeout()):
        assert EmailService("ada@example.com", background=False).send_raw_mail("Hi", "<p>Hi</p>") is False


# --- Celery task retries (eager: retries run inline, without the backoff wait) ----


@pytest.fixture
def eager_retries(monkeypatch):
    # With task_eager_propagates on, an eager task raises Retry instead of re-running
    # itself, so switch it off to exercise the retry loop as a worker would.
    monkeypatch.setitem(celery_app.conf, "CELERY_TASK_EAGER_PROPAGATES", False)


@pytest.mark.parametrize("status_code", [500, 502, 503, 429])
def test_task_retries_on_5xx_and_429(brevo_key, eager_retries, status_code):
    with mock.patch(POST, side_effect=[brevo_response(status_code), brevo_response(201)]) as post:
        result = send_email_task.delay(**task_kwargs())

    assert post.call_count == 2
    assert result.get() is True


def test_task_retries_on_network_error(brevo_key, eager_retries):
    with mock.patch(POST, side_effect=[requests.ConnectionError(), brevo_response(201)]) as post:
        send_email_task.delay(**task_kwargs())

    assert post.call_count == 2


@pytest.mark.parametrize("status_code", [400, 401, 403, 404])
def test_task_does_not_retry_on_4xx(brevo_key, caplog, status_code):
    with mock.patch(POST, return_value=brevo_response(status_code, "bad request body")) as post:
        result = send_email_task.delay(**task_kwargs())

    assert post.call_count == 1
    assert result.get() is False
    assert "bad request body" in caplog.text


def test_task_gives_up_after_max_retries(brevo_key, eager_retries, caplog):
    with mock.patch(POST, return_value=brevo_response(503)) as post:
        result = send_email_task.delay(**task_kwargs())

    assert post.call_count == 6  # first try + max_retries=5
    assert result.failed()
    assert isinstance(result.result, TransientEmailError)
    assert "Giving up on email to ada@example.com" in caplog.text


# --- Queued after commit ----------------------------------------------------


@pytest.mark.django_db
def test_background_send_is_queued_only_after_commit(django_capture_on_commit_callbacks):
    with mock.patch(APPLY_ASYNC) as apply_async:
        with django_capture_on_commit_callbacks(execute=True):
            with transaction.atomic():
                assert EmailService("ada@example.com").send_raw_mail("Hi", "<p>Hi</p>") is True
            apply_async.assert_not_called()

    apply_async.assert_called_once_with(
        kwargs={
            "email": "ada@example.com",
            "subject": "Hi",
            "html": "<p>Hi</p>",
            "text": "Hi",
            "transport": "brevo",
        },
        countdown=None,
    )


@pytest.mark.django_db
def test_rolled_back_transaction_queues_nothing(django_capture_on_commit_callbacks):
    with mock.patch(APPLY_ASYNC) as apply_async:
        with django_capture_on_commit_callbacks(execute=True) as callbacks:
            with pytest.raises(RuntimeError):
                with transaction.atomic():
                    EmailService("ada@example.com").send_raw_mail("Hi", "<p>Hi</p>")
                    raise RuntimeError("write failed")

    assert callbacks == []
    apply_async.assert_not_called()


@pytest.mark.django_db
def test_broker_outage_sends_the_email_from_this_process(
    brevo, inline_background, django_capture_on_commit_callbacks, caplog
):
    with mock.patch(APPLY_ASYNC, side_effect=OperationalError("redis down")):
        with django_capture_on_commit_callbacks(execute=True):
            assert EmailService("ada@example.com").send_raw_mail("Hi", "<p>Hi</p>") is True

    assert "Could not queue accounts.tasks.send_email_task on Celery" in caplog.text
    assert [p["to"] for p in brevo.payloads] == [[{"email": "ada@example.com"}]]


@pytest.mark.django_db
def test_background_send_reaches_brevo_through_the_task(brevo, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        EmailService("ada@example.com").send_raw_mail("Hi", "<p>Hi</p>")

    assert [p["to"] for p in brevo.payloads] == [[{"email": "ada@example.com"}]]


# --- Templates --------------------------------------------------------------


def test_template_mail_injects_context_and_sends_text_version(brevo):
    url = "http://frontend.test/reset-password?uid=MQ&token=abc-123"

    ok = EmailService("ada@example.com", background=False).send_template_mail(
        "Reset", "emails/password_reset.html", {"name": "Ada", "action_url": url}
    )

    assert ok is True
    [payload] = brevo.payloads
    assert payload["subject"] == "Reset"
    html, text = payload["htmlContent"], payload["textContent"]
    assert 'href="http://frontend.test/reset-password?uid=MQ&amp;token=abc-123"' in html
    assert "support@flashform.test" in html  # SUPPORT_EMAIL from settings
    assert "Flashform" in html
    # The plain-text part has the raw link (entities decoded) and no markup.
    assert url in text
    assert "Hi Ada," in text
    assert "<" not in text
    assert "\n\n\n" not in text


def test_template_mail_with_unknown_template_returns_false(brevo, caplog):
    ok = EmailService("ada@example.com", background=False).send_template_mail(
        "Oops", "emails/missing.html", {}
    )

    assert ok is False
    assert len(brevo) == 0
    assert "Error rendering email template emails/missing.html" in caplog.text


def test_html_to_text_drops_head_and_decodes_entities():
    html = "<html><head><title>T</title></head><body><p>A &amp; B</p>\n\n\n\n<p>C</p></body></html>"

    assert html_to_text(html) == "A & B\n\nC"


# --- SMTP transport ---------------------------------------------------------


def test_smtp_mail_inline_uses_django_mail():
    ok = EmailService("ada@example.com", background=False).send_smtp_mail("Hi", "<p>Hi</p>", "Hi")

    assert ok is True
    [message] = mail.outbox
    assert message.to == ["ada@example.com"]
    assert message.body == "Hi"
    assert message.alternatives[0][0] == "<p>Hi</p>"


@pytest.mark.django_db
def test_smtp_mail_in_background_goes_through_the_task(django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        EmailService("ada@example.com").send_smtp_mail("Hi", "<p>Hi</p>")

    assert [m.subject for m in mail.outbox] == ["Hi"]
