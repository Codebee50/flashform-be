"""WebSocket consumers end to end through the real ASGI app (origin check, JWT middleware,
routing). Transactional DB: consumers query from another thread, and broadcasts only go
out once the transaction really commits."""

from datetime import timedelta

import pytest
from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.layers import get_channel_layer
from channels.testing import WebsocketCommunicator
from django.conf import settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from activities import services
from activities.models import Activity, Participant
from flashform.asgi import application
from quizzes.models import Quiz
from rooms.models import Room

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]

db = database_sync_to_async


@pytest.fixture(autouse=True)
def _fresh_channel_layer():
    async_to_sync(get_channel_layer().flush)()
    yield
    async_to_sync(get_channel_layer().flush)()


@pytest.fixture
def room(teacher):
    return Room.objects.create(owner=teacher, name="Period 3 Biology", code="BIO3AB")


@pytest.fixture
def other_teacher(django_user_model):
    return django_user_model.objects.create_user(
        username="bob@example.com", email="bob@example.com", password="x"
    )


def socket(path: str) -> WebsocketCommunicator:
    origin = settings.CORS_ALLOWED_ORIGINS[0].encode()
    return WebsocketCommunicator(application, path, headers=[(b"origin", origin)])


def teacher_socket(room, token) -> WebsocketCommunicator:
    return socket(f"/ws/teacher/room/{room.id}/?auth={token}")


def student_socket(code="BIO3AB", token=None) -> WebsocketCommunicator:
    return socket(f"/ws/room/{code}/" + (f"?token={token}" if token else ""))


async def connected(communicator) -> WebsocketCommunicator:
    ok, _ = await communicator.connect()
    assert ok
    assert await communicator.receive_nothing(timeout=0.05), "socket was closed"
    return communicator


async def close_code(communicator) -> int:
    ok, _ = await communicator.connect()
    assert ok, "rejections accept first so the client sees the close code"
    output = await communicator.receive_output(timeout=1)
    assert output["type"] == "websocket.close"
    return output["code"]


def teacher_api(teacher) -> APIClient:
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {AccessToken.for_user(teacher)}")
    return client


def join(room, name="Grace"):
    """Join through the service; returns (participant, raw token)."""
    result = services.join_room(room.code, name)
    return result.participant, result.token


# --- Teacher socket ---------------------------------------------------------


async def test_submitted_response_notifies_the_teacher(teacher, room):
    activity = await db(services.start_quick_activity)(room, type="MC")
    participant, token = await db(join)(room)
    question = await db(activity.questions.get)()
    teacher_ws = await connected(teacher_socket(room, AccessToken.for_user(teacher)))

    student = APIClient()
    student.credentials(HTTP_X_PARTICIPANT_TOKEN=token)
    response = await db(student.put)(
        f"/api/participant/responses/{question.id}", {"choice_index": 2}, format="json"
    )
    assert response.status_code == 200

    version = (await db(Activity.objects.get)(pk=activity.pk)).version
    assert await teacher_ws.receive_json_from(timeout=1) == {
        "type": "responses_updated",
        "activity_id": activity.id,
        "version": version,
    }
    await teacher_ws.disconnect()


async def test_join_and_leave_notify_the_teacher(teacher, room):
    activity = await db(services.start_quick_activity)(room, type="TF")
    teacher_ws = await connected(teacher_socket(room, AccessToken.for_user(teacher)))

    participant, _ = await db(join)(room)
    event = await teacher_ws.receive_json_from(timeout=1)
    assert event == {"type": "participants_changed", "activity_id": activity.id, "version": 1}

    await db(services.leave)(participant)
    # Throttled (within 500 ms of the join): Celery runs eagerly in tests, so the trailing
    # event arrives straight away with the latest version.
    event = await teacher_ws.receive_json_from(timeout=1)
    assert event == {"type": "participants_changed", "activity_id": activity.id, "version": 2}
    await teacher_ws.disconnect()


async def test_teacher_sees_activity_updates(teacher, room):
    teacher_ws = await connected(teacher_socket(room, AccessToken.for_user(teacher)))

    activity = await db(services.start_quick_activity)(room, type="MC")
    assert await teacher_ws.receive_json_from(timeout=1) == {
        "type": "activity_updated",
        "activity_id": activity.id,
        "version": 0,
    }
    await db(services.end_activity)(activity)
    assert await teacher_ws.receive_json_from(timeout=1) == {
        "type": "activity_updated",
        "activity_id": activity.id,
        "version": 1,
    }
    await teacher_ws.disconnect()


@pytest.mark.parametrize("token", ["", "not-a-jwt", "expired", "refresh"])
async def test_bad_teacher_token_is_rejected_with_4401(teacher, room, token):
    if token == "expired":
        access = AccessToken.for_user(teacher)
        access.set_exp(from_time=timezone.now() - timedelta(hours=1))
        token = str(access)
    elif token == "refresh":
        # Right signature, wrong token type. for_user() saves an OutstandingToken row.
        token = str(await db(RefreshToken.for_user)(teacher))

    assert await close_code(teacher_socket(room, token)) == 4401


async def test_inactive_teacher_is_rejected_with_4401(teacher, room):
    token = AccessToken.for_user(teacher)
    teacher.is_active = False
    await db(teacher.save)()

    assert await close_code(teacher_socket(room, token)) == 4401


async def test_other_teachers_room_is_rejected_with_4404(other_teacher, room):
    assert await close_code(teacher_socket(room, AccessToken.for_user(other_teacher))) == 4404


async def test_missing_room_is_rejected_with_4404(teacher):
    communicator = socket(f"/ws/teacher/room/999999/?auth={AccessToken.for_user(teacher)}")
    assert await close_code(communicator) == 4404


async def test_foreign_origin_is_refused(teacher, room):
    communicator = WebsocketCommunicator(
        application,
        f"/ws/teacher/room/{room.id}/?auth={AccessToken.for_user(teacher)}",
        headers=[(b"origin", b"https://evil.example")],
    )
    ok, _ = await communicator.connect()
    assert not ok


# --- Student socket ---------------------------------------------------------


async def test_waiting_screen_student_gets_activity_started(teacher, room):
    student_ws = await connected(student_socket("bio3ab"))  # no token, lowercase code

    response = await db(teacher_api(teacher).post)(
        f"/api/rooms/{room.id}/activities", {"type": "QUICK", "question": {"type": "MC"}}, format="json"
    )
    assert response.status_code == 201

    assert await student_ws.receive_json_from(timeout=1) == {
        "type": "activity_started",
        "activity_id": response.json()["activity"]["id"],
        "version": 0,
    }
    await student_ws.disconnect()


async def test_students_see_the_old_activity_end_before_the_new_one_starts(room):
    old = await db(services.start_quick_activity)(room, type="MC")
    _, token = await db(join)(room)
    student_ws = await connected(student_socket(token=token))

    new = await db(services.start_quick_activity)(room, type="SA")

    assert await student_ws.receive_json_from(timeout=1) == {
        "type": "activity_ended",
        "activity_id": old.id,
        "version": 2,
    }
    assert await student_ws.receive_json_from(timeout=1) == {
        "type": "activity_started",
        "activity_id": new.id,
        "version": 0,
    }
    await student_ws.disconnect()


async def test_quiz_start_and_navigate_notify_students_and_teacher(teacher, room):
    quiz = await db(Quiz.objects.create)(owner=teacher, title="Week 1")
    for order in range(2):
        await db(quiz.questions.create)(order=order, type="TF", prompt=f"Q{order}", correct_index=0)
    api = teacher_api(teacher)
    student_ws = await connected(student_socket())
    teacher_ws = await connected(teacher_socket(room, AccessToken.for_user(teacher)))

    response = await db(api.post)(
        f"/api/rooms/{room.id}/activities",
        {"type": "QUIZ", "quiz_id": quiz.id, "mode": "TEACHER_PACED"},
        format="json",
    )
    assert response.status_code == 201
    activity_id = response.json()["activity"]["id"]
    started = {"activity_id": activity_id, "version": 0}
    assert await student_ws.receive_json_from(timeout=1) == {"type": "activity_started", **started}
    assert await teacher_ws.receive_json_from(timeout=1) == {"type": "activity_updated", **started}

    response = await db(api.post)(
        f"/api/activities/{activity_id}/navigate", {"index": 1}, format="json"
    )
    assert response.status_code == 200
    moved = {"type": "activity_updated", "activity_id": activity_id, "version": 1}
    assert await student_ws.receive_json_from(timeout=1) == moved
    assert await teacher_ws.receive_json_from(timeout=1) == moved

    # A retried navigate to the same question is a no-op: no event.
    await db(api.post)(f"/api/activities/{activity_id}/navigate", {"index": 1}, format="json")
    assert await student_ws.receive_nothing(timeout=0.2)
    assert await teacher_ws.receive_nothing(timeout=0.05)
    await student_ws.disconnect()
    await teacher_ws.disconnect()


async def test_students_do_not_get_teacher_events(room):
    activity = await db(services.start_quick_activity)(room, type="MC")
    student_ws = await connected(student_socket())

    participant, _ = await db(join)(room)
    question = await db(activity.questions.get)()
    await db(services.submit_response)(participant, question.id, choice_index=0)

    assert await student_ws.receive_nothing(timeout=0.2)
    await db(services.end_activity)(activity)
    assert (await student_ws.receive_json_from(timeout=1))["type"] == "activity_ended"
    await student_ws.disconnect()


async def test_student_socket_rejections(teacher, room):
    await db(services.start_quick_activity)(room, type="MC")
    other_room = await db(Room.objects.create)(owner=teacher, name="Other", code="OTHER1")
    await db(services.start_quick_activity)(other_room, type="MC")
    foreign, foreign_token = await db(join)(other_room)
    removed, removed_token = await db(join)(room)
    await db(services.leave)(removed)

    assert await close_code(student_socket("NOPE99")) == 4404
    assert await close_code(student_socket(token="garbage")) == 4401
    assert await close_code(student_socket(token=foreign_token)) == 4401
    assert await close_code(student_socket(token=removed_token)) == 4401


async def test_token_from_an_earlier_activity_in_the_room_still_connects(room):
    activity = await db(services.start_quick_activity)(room, type="MC")
    _, token = await db(join)(room)
    await db(services.end_activity)(activity)

    student_ws = await connected(student_socket(token=token))
    await student_ws.disconnect()


# --- Client messages --------------------------------------------------------


async def test_ping_gets_pong_and_everything_else_is_ignored(room):
    student_ws = await connected(student_socket())

    await student_ws.send_json_to({"type": "ping"})
    assert await student_ws.receive_json_from(timeout=1) == {"type": "pong"}

    await student_ws.send_json_to({"type": "submit", "choice_index": 1})
    await student_ws.send_to(text_data="{not json")
    await student_ws.send_to(text_data='["ping"]')
    await student_ws.send_to(bytes_data=b"\x00\x01")
    assert await student_ws.receive_nothing(timeout=0.2)

    # Still open.
    await student_ws.send_json_to({"type": "ping"})
    assert await student_ws.receive_json_from(timeout=1) == {"type": "pong"}
    await student_ws.disconnect()


async def test_teacher_ping_gets_pong(teacher, room):
    teacher_ws = await connected(teacher_socket(room, AccessToken.for_user(teacher)))

    await teacher_ws.send_json_to({"type": "ping"})

    assert await teacher_ws.receive_json_from(timeout=1) == {"type": "pong"}
    await teacher_ws.disconnect()


async def test_ping_updates_last_seen_at_most_every_30_seconds(room):
    await db(services.start_quick_activity)(room, type="MC")
    participant, token = await db(join)(room)
    long_ago = timezone.now() - timedelta(minutes=5)
    await db(Participant.objects.filter(pk=participant.pk).update)(last_seen_at=long_ago)

    student_ws = await connected(student_socket(token=token))  # connecting counts
    after_connect = (await db(Participant.objects.get)(pk=participant.pk)).last_seen_at
    assert after_connect > long_ago

    await student_ws.send_json_to({"type": "ping"})
    await student_ws.receive_json_from(timeout=1)
    assert (await db(Participant.objects.get)(pk=participant.pk)).last_seen_at == after_connect

    # 31 s later, the next ping writes again.
    earlier = after_connect - timedelta(seconds=31)
    await db(Participant.objects.filter(pk=participant.pk).update)(last_seen_at=earlier)
    await student_ws.send_json_to({"type": "ping"})
    await student_ws.receive_json_from(timeout=1)
    assert (await db(Participant.objects.get)(pk=participant.pk)).last_seen_at > earlier
    await student_ws.disconnect()
