"""flashform.background: tasks go to Celery with USE_CELERY on, and run in a thread of the
web process with it off (or when the broker can't take them), retrying as a worker would."""

import threading
import time
from unittest import mock

import pytest
import requests
from celery import shared_task
from kombu.exceptions import OperationalError

from activities import broadcast, services
from activities.broadcast import RESPONSES_UPDATED
from activities.models import Activity
from flashform import background
from rooms.models import Room

calls: list = []
echoed = threading.Event()


@shared_task(name="tests.echo")
def echo(value):
    calls.append((value, threading.current_thread().name))
    echoed.set()


@shared_task(
    name="tests.flaky",
    autoretry_for=(ConnectionError,),
    retry_backoff=True,
    retry_jitter=False,
    max_retries=3,
)
def flaky(failures: int):
    """Fails `failures` times, then succeeds."""
    calls.append("attempt")
    if calls.count("attempt") <= failures:
        raise ConnectionError("down")
    return "ok"


@shared_task(name="tests.boom")
def boom():
    calls.append("attempt")
    raise ValueError("bug")


@pytest.fixture(autouse=True)
def _reset_calls():
    calls.clear()
    echoed.clear()


# --- Choosing Celery or a thread --------------------------------------------


def test_with_celery_tasks_go_to_the_broker(settings, inline_background):
    settings.USE_CELERY = True
    with mock.patch.object(echo, "apply_async") as apply_async:
        background.submit(echo, {"value": 1})
        background.submit(echo, {"value": 2}, countdown=0.5)

    assert apply_async.call_args_list == [
        mock.call(kwargs={"value": 1}, countdown=None),
        mock.call(kwargs={"value": 2}, countdown=0.5),
    ]
    assert inline_background.delays == []
    assert calls == []


def test_without_celery_the_broker_is_never_used(no_celery):
    with mock.patch.object(echo, "apply_async") as apply_async:
        background.submit(echo, {"value": 1})
        background.submit(echo, {"value": 2}, countdown=0.5)

    apply_async.assert_not_called()
    assert [value for value, _ in calls] == [1, 2]
    assert no_celery.delays == [0, 0.5]


def test_a_broker_outage_falls_back_to_a_thread(settings, inline_background, caplog):
    settings.USE_CELERY = True
    with mock.patch.object(echo, "apply_async", side_effect=OperationalError("redis down")):
        background.submit(echo, {"value": 1})

    assert [value for value, _ in calls] == [1]
    assert "Could not queue tests.echo on Celery; running it in this process" in caplog.text


# --- Retries in a thread ----------------------------------------------------


def test_retries_with_the_tasks_backoff_until_it_succeeds(no_celery):
    assert background.run_with_retries(flaky, {"failures": 2}) == "ok"

    assert calls.count("attempt") == 3
    assert no_celery.sleeps == [1, 2]  # retry_backoff, no jitter


def test_gives_up_after_max_retries(no_celery, caplog):
    assert background.run_with_retries(flaky, {"failures": 99}) is None

    assert calls.count("attempt") == 4  # first try + max_retries=3
    assert no_celery.sleeps == [1, 2, 4]
    assert "Task tests.flaky failed after 3 retries: down" in caplog.text


def test_other_errors_are_logged_and_not_retried(no_celery, caplog):
    background.submit(boom, {})  # never raises

    assert calls == ["attempt"]
    assert no_celery.sleeps == []
    assert "Task tests.boom failed" in caplog.text


def test_email_retries_and_gives_up_like_the_worker(no_celery, brevo, caplog):
    """The email task's own policy applies: 5xx/429 retried 5 times with backoff, then its
    on_failure reports the retry count."""
    from accounts.tasks import send_email_task

    brevo.post.return_value = mock.Mock(status_code=503, text="unavailable")
    background.submit(
        send_email_task, {"email": "ada@example.com", "subject": "Hi", "html": "<p>Hi</p>"}
    )

    assert len(brevo) == 6
    assert len(no_celery.sleeps) == 5
    # Full jitter: each wait is random, up to 1, 2, 4, 8, 16 seconds.
    assert all(0 <= wait <= 2**n for n, wait in enumerate(no_celery.sleeps))
    assert "Giving up on email to ada@example.com (subject 'Hi') after 5 retries" in caplog.text


def test_network_errors_are_retried_in_a_thread(no_celery, brevo):
    from accounts.tasks import send_email_task

    accepted = brevo.post.return_value
    brevo.post.side_effect = [requests.ConnectionError(), accepted]

    background.submit(send_email_task, {"email": "ada@example.com", "subject": "Hi", "html": "x"})

    assert len(brevo) == 2
    assert len(no_celery.sleeps) == 1


# --- Real threads -----------------------------------------------------------


def test_tasks_run_on_the_pool_after_their_countdown(settings):
    settings.USE_CELERY = False
    started = time.monotonic()

    background.submit(echo, {"value": 1}, countdown=0.05)

    assert echoed.wait(timeout=5)
    assert time.monotonic() - started >= 0.05
    [(value, thread_name)] = calls
    assert value == 1
    assert thread_name.startswith("background")


# --- The app's tasks without Celery -----------------------------------------


@pytest.mark.django_db
def test_registration_email_is_sent_without_celery(no_celery, brevo, post_committed):
    from accounts.tasks import send_email_task

    with mock.patch.object(send_email_task, "apply_async") as apply_async:
        response = post_committed(
            "/api/auth/register",
            {"name": "Ada", "email": "ada@example.com", "password": "correct-horse-battery"},
        )

    assert response.status_code == 201
    apply_async.assert_not_called()
    assert [payload["to"] for payload in brevo.payloads] == [[{"email": "ada@example.com"}]]
    assert brevo.last_link()[0] == "/verify-email"


@pytest.mark.django_db
def test_trailing_teacher_event_is_sent_without_celery(no_celery, teacher):
    room = Room.objects.create(owner=teacher, name="Period 3 Biology", code="BIO3AB")
    activity = services.start_quick_activity(room, type="MC")
    Activity.objects.filter(pk=activity.pk).update(version=7)

    with mock.patch("activities.broadcast.send") as send:
        broadcast.send_throttled(RESPONSES_UPDATED, room.id, activity.id, 1)
        broadcast.send_throttled(RESPONSES_UPDATED, room.id, activity.id, 2)  # throttled

    group = f"teacher.room.{room.id}"
    assert [call.args for call in send.call_args_list] == [
        (group, {"type": RESPONSES_UPDATED, "activity_id": activity.id, "version": 1}),
        # The trailing event, with the activity's latest version.
        (group, {"type": RESPONSES_UPDATED, "activity_id": activity.id, "version": 7}),
    ]
    assert no_celery.delays == [0.5]
