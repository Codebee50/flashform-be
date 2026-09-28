"""Broadcast throttling: at most one teacher event per 500 ms per activity, one trailing
task per burst, and the last change of a burst is never missed (PRD §6 rule 6, §9)."""

import os
import time
import uuid
from unittest import mock

import pytest

from activities import broadcast, services
from activities.broadcast import PARTICIPANTS_CHANGED, RESPONSES_UPDATED
from activities.models import Activity
from activities.tasks import send_trailing_event
from rooms.models import Room

pytestmark = pytest.mark.django_db


@pytest.fixture
def room(teacher):
    return Room.objects.create(owner=teacher, name="Period 3 Biology", code="BIO3AB")


@pytest.fixture
def activity(room):
    return services.start_quick_activity(room, type="MC")


@pytest.fixture
def sent():
    """Events handed to the channel layer, as (group, payload)."""
    with mock.patch("activities.broadcast.send") as send:
        yield send


@pytest.fixture
def scheduled():
    with mock.patch("activities.tasks.send_trailing_event.apply_async") as apply_async:
        yield apply_async


def events(send_mock) -> list[tuple[str, dict]]:
    return [call.args for call in send_mock.call_args_list]


def test_a_burst_sends_once_and_schedules_one_trailing_task(activity, sent, scheduled):
    for version in range(1, 21):
        broadcast.send_throttled(RESPONSES_UPDATED, activity.room_id, activity.id, version)

    assert events(sent) == [
        (
            f"teacher.room.{activity.room_id}",
            {"type": "responses_updated", "activity_id": activity.id, "version": 1},
        )
    ]
    scheduled.assert_called_once_with(args=[RESPONSES_UPDATED, activity.id], countdown=0.5)


def test_trailing_event_sends_the_latest_version_and_reopens_scheduling(activity, sent, scheduled):
    broadcast.send_throttled(RESPONSES_UPDATED, activity.room_id, activity.id, 1)
    broadcast.send_throttled(RESPONSES_UPDATED, activity.room_id, activity.id, 2)
    Activity.objects.filter(pk=activity.pk).update(version=7)

    send_trailing_event(RESPONSES_UPDATED, activity.id)

    assert events(sent)[-1] == (
        f"teacher.room.{activity.room_id}",
        {"type": "responses_updated", "activity_id": activity.id, "version": 7},
    )
    # The trailing send holds the lock again, so a change right after it is throttled and
    # gets its own trailing task: nothing is missed.
    broadcast.send_throttled(RESPONSES_UPDATED, activity.room_id, activity.id, 8)
    assert len(events(sent)) == 2
    assert scheduled.call_count == 2


def test_after_the_window_the_next_event_goes_out_immediately(activity, sent, scheduled):
    broadcast.send_throttled(RESPONSES_UPDATED, activity.room_id, activity.id, 1)
    time.sleep(broadcast.THROTTLE_MS / 1000 + 0.05)
    broadcast.send_throttled(RESPONSES_UPDATED, activity.room_id, activity.id, 2)

    assert [payload["version"] for _, payload in events(sent)] == [1, 2]
    scheduled.assert_not_called()


def test_throttles_are_per_activity_and_per_event_type(room, activity, teacher, sent, scheduled):
    other_room = Room.objects.create(owner=teacher, name="Other", code="OTHER1")
    other = services.start_quick_activity(other_room, type="MC")

    broadcast.send_throttled(RESPONSES_UPDATED, room.id, activity.id, 1)
    broadcast.send_throttled(PARTICIPANTS_CHANGED, room.id, activity.id, 1)
    broadcast.send_throttled(RESPONSES_UPDATED, other_room.id, other.id, 1)

    assert len(events(sent)) == 3
    scheduled.assert_not_called()


def test_submissions_only_broadcast_after_commit_and_are_throttled(
    activity, sent, scheduled, django_capture_on_commit_callbacks
):
    participants = [services.join_room("BIO3AB", f"S{i}").participant for i in range(5)]
    question = activity.questions.get()
    sent.reset_mock()
    scheduled.reset_mock()

    with django_capture_on_commit_callbacks() as callbacks:
        for participant in participants:
            services.submit_response(participant, question.id, choice_index=0)
    assert not sent.called  # nothing before commit

    for callback in callbacks:
        callback()

    assert [payload["type"] for _, payload in events(sent)] == ["responses_updated"]
    scheduled.assert_called_once()


def test_a_failed_broadcast_never_breaks_the_request(activity, django_capture_on_commit_callbacks):
    token = services.join_room("BIO3AB", "Grace").token
    question = activity.questions.get()

    from rest_framework.test import APIClient

    client = APIClient()
    client.credentials(HTTP_X_PARTICIPANT_TOKEN=token)
    with mock.patch("activities.broadcast.get_channel_layer", side_effect=ConnectionError):
        with django_capture_on_commit_callbacks(execute=True):
            response = client.put(
                f"/api/participant/responses/{question.id}", {"choice_index": 1}, format="json"
            )

    assert response.status_code == 200


def test_trailing_task_rejects_unthrottled_event_types(activity):
    with pytest.raises(ValueError):
        send_trailing_event("activity_ended", activity.id)


def test_trailing_task_for_a_deleted_activity_is_a_no_op(activity, sent):
    activity_id = activity.id
    activity.delete()

    send_trailing_event(RESPONSES_UPDATED, activity_id)

    sent.assert_not_called()


def test_memory_store_set_nx_px():
    store = broadcast.MemoryStore()

    assert store.set("k", px=50, nx=True)
    assert not store.set("k", px=50, nx=True)
    time.sleep(0.06)
    assert store.set("k", px=50, nx=True)
    store.delete("k")
    assert store.set("k", px=50, nx=True)


def test_redis_store_set_nx_px():
    """Against the compose Redis, with unique keys that expire by themselves (no flush)."""
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        pytest.skip("REDIS_URL not set")
    store = broadcast.RedisStore(url)
    key = f"test:{uuid.uuid4()}"
    try:
        store._redis.ping()
    except Exception:
        pytest.skip("Redis not reachable")

    assert store.set(key, px=100, nx=True)
    assert not store.set(key, px=100, nx=True)
    store.delete(key)
    assert store.set(key, px=100, nx=True)
    time.sleep(0.15)
    assert store.set(key, px=100, nx=True)
    store.delete(key)
