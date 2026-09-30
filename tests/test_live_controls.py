"""P2 live-room controls: room lock on join (PRD R4), removing a participant (L5) and
Hide results (L4)."""

import uuid
from unittest import mock

import pytest
from rest_framework.test import APIClient

from activities import services
from activities.models import Activity, Participant, Response
from rooms.models import Room

from .test_activities import (
    STATE,
    join,
    join_url,
    question,
    refresh,
    response_url,
    teacher_state_url,
)

pytestmark = pytest.mark.django_db


def remove_url(activity, participant_id) -> str:
    return f"/api/activities/{activity.id}/participants/{participant_id}"


def activity_url(activity) -> str:
    return f"/api/activities/{activity.id}"


def lock(room, locked=True) -> None:
    Room.objects.filter(pk=room.pk).update(is_locked=locked)


def rejoin(student, code="BIO3AB", name="Grace", ip="10.0.0.1"):
    """POST /join sending the student's current token, as the waiting screen does."""
    return student.post(join_url(code), {"name": name}, format="json", REMOTE_ADDR=ip)


@pytest.fixture
def room(teacher):
    return Room.objects.create(owner=teacher, name="Period 3 Biology", code="BIO3AB")


@pytest.fixture
def quick(room):
    return services.start_quick_activity(room, type="MC")


@pytest.fixture
def other_client(django_user_model):
    from rest_framework_simplejwt.tokens import AccessToken

    from accounts.services import mark_email_verified

    user = django_user_model.objects.create_user(
        username="bob@example.com", email="bob@example.com", password="x"
    )
    mark_email_verified(user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {AccessToken.for_user(user)}")
    return client


@pytest.fixture
def sent():
    """Events handed to the channel layer, as (group, payload)."""
    with mock.patch("activities.broadcast.send") as send:
        yield send


def payloads(send_mock) -> list[tuple[str, dict]]:
    return [call.args for call in send_mock.call_args_list]


# --- Room lock (R4) ---------------------------------------------------------


def test_locking_through_the_api_refuses_new_students(teacher_client, api_client, room, quick):
    teacher_client.patch(f"/api/rooms/{room.id}", {"is_locked": True}, format="json")

    response = api_client.post(join_url("BIO3AB"), {"name": "Late"}, format="json")

    assert response.status_code == 423
    assert response.json() == {"detail": "Room is locked.", "code": "room_locked"}
    assert not Participant.objects.exists()

    teacher_client.patch(f"/api/rooms/{room.id}", {"is_locked": False}, format="json")
    assert api_client.post(join_url("BIO3AB"), {"name": "Late"}, format="json").status_code == 201


def test_students_already_in_keep_answering_after_the_lock(room, quick):
    student = join()
    lock(room)

    response = student.put(response_url(question(quick)), {"choice_index": 1}, format="json")

    assert response.status_code == 200
    assert student.get(STATE).status_code == 200


def test_already_joined_students_rejoin_the_next_activity_while_locked(room, quick):
    """PRD §7: the waiting screen re-joins with the saved name when a new activity starts.
    A lock must not shut out students who were already in (PRD R4)."""
    student = join()
    lock(room)
    next_activity = services.start_quick_activity(room, type="TF")

    response = rejoin(student)

    assert response.status_code == 201, response.json()
    assert response.json()["activity_id"] == next_activity.id
    assert Participant.objects.filter(activity=next_activity, name="Grace").exists()


def test_rejoining_the_current_activity_while_locked_is_allowed(room, quick):
    student = join()
    lock(room)

    assert rejoin(student).status_code == 201


@pytest.mark.parametrize("how", ["garbage", "other_room", "left", "removed"])
def test_tokens_that_dont_bypass_the_lock(teacher, room, quick, how):
    if how == "garbage":
        student = APIClient()
        student.credentials(HTTP_X_PARTICIPANT_TOKEN="not-a-real-token")
    elif how == "other_room":
        other = Room.objects.create(owner=teacher, name="Other", code="OTHER1")
        services.start_quick_activity(other, type="MC")
        student = join(code="OTHER1")
    else:
        student = join()
        participant = Participant.objects.get(pk=student.participant_id)
        if how == "left":
            services.leave(participant)
        else:
            services.remove_participant(quick, participant.id)
    lock(room)
    count = Participant.objects.count()

    response = rejoin(student)

    assert response.status_code == 423
    assert response.json()["code"] == "room_locked"
    assert Participant.objects.count() == count


# --- Remove participant (L5) ------------------------------------------------


def test_remove_participant(teacher_client, quick):
    kept = join(name="Grace", ip="10.0.0.1")
    removed = join(name="Troll", ip="10.0.0.2")
    removed.put(response_url(question(quick)), {"choice_index": 3}, format="json")
    version = refresh(quick).version

    response = teacher_client.delete(remove_url(quick, removed.participant_id))

    assert response.status_code == 204
    assert response.content == b""
    assert Participant.objects.get(pk=removed.participant_id).is_removed
    assert refresh(quick).version == version + 1
    # Their token stops working.
    assert removed.get(STATE).status_code == 401
    submit = removed.put(response_url(question(quick)), {"choice_index": 0}, format="json")
    assert submit.status_code == 401
    assert submit.json()["code"] == "invalid_participant_token"
    # Gone from the live view; the answer stays in the DB but isn't counted.
    body = teacher_client.get(teacher_state_url(quick)).json()
    assert [p["name"] for p in body["participants"]] == ["Grace"]
    assert body["responses"] == []
    assert body["summaries"][0]["choice_counts"] == [0, 0, 0, 0]
    assert Response.objects.count() == 1
    # Everyone else is unaffected.
    assert kept.get(STATE).status_code == 200


def test_removing_twice_is_a_no_op(teacher_client, quick):
    student = join()
    teacher_client.delete(remove_url(quick, student.participant_id))
    version = refresh(quick).version

    again = teacher_client.delete(remove_url(quick, student.participant_id))

    assert again.status_code == 204
    assert refresh(quick).version == version


def test_removing_a_student_who_left_is_a_no_op(teacher_client, quick):
    student = join()
    student.post("/api/participant/leave")
    version = refresh(quick).version

    assert teacher_client.delete(remove_url(quick, student.participant_id)).status_code == 204
    assert refresh(quick).version == version


def test_removed_student_can_join_again_while_unlocked(teacher_client, quick):
    student = join()
    teacher_client.delete(remove_url(quick, student.participant_id))

    again = rejoin(student)

    assert again.status_code == 201
    assert Participant.objects.filter(is_removed=False).count() == 1


def test_remove_after_the_end_drops_them_from_the_report(teacher_client, room, quick):
    kept, removed = join(name="Grace", ip="10.0.0.1"), join(name="Troll", ip="10.0.0.2")
    services.end_activity(quick)

    response = teacher_client.delete(remove_url(quick, removed.participant_id))

    assert response.status_code == 204
    body = teacher_client.get(teacher_state_url(quick)).json()
    assert [p["name"] for p in body["participants"]] == ["Grace"]
    [report] = teacher_client.get("/api/activities").json()
    assert report["participant_count"] == 1
    # Their waiting-screen token no longer gets them past a lock.
    lock(room)
    services.start_quick_activity(room, type="TF")
    assert rejoin(removed, name="Troll", ip="10.0.0.2").status_code == 423
    assert rejoin(kept).status_code == 201


@pytest.mark.parametrize("which", ["unknown", "other_activity"])
def test_remove_unknown_participant_is_404(teacher_client, room, quick, which):
    if which == "unknown":
        participant_id = uuid.uuid4()
    else:
        student = join()
        participant_id = student.participant_id
        quick = services.start_quick_activity(room, type="TF")  # a different activity

    response = teacher_client.delete(remove_url(quick, participant_id))

    assert response.status_code == 404
    assert response.json()["code"] == "participant_not_found"
    assert not Participant.objects.filter(is_removed=True).exists()


def test_remove_with_a_malformed_id_is_404(teacher_client, quick):
    response = teacher_client.delete(remove_url(quick, "not-a-uuid"))

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_remove_needs_the_owner(api_client, other_client, quick):
    student = join()

    assert api_client.delete(remove_url(quick, student.participant_id)).status_code == 401
    response = other_client.delete(remove_url(quick, student.participant_id))
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    assert not Participant.objects.get().is_removed


def test_remove_broadcasts_participant_removed_after_commit(
    teacher_client, room, quick, sent, django_capture_on_commit_callbacks
):
    student = join()
    sent.reset_mock()

    with django_capture_on_commit_callbacks() as callbacks:
        teacher_client.delete(remove_url(quick, student.participant_id))
    assert not sent.called
    for callback in callbacks:
        callback()

    version = refresh(quick).version
    assert payloads(sent) == [
        (
            "room.BIO3AB",
            {
                "type": "participant_removed",
                "activity_id": quick.id,
                "version": version,
                "participant_id": student.participant_id,
            },
        ),
        (
            f"teacher.room.{room.id}",
            {"type": "participants_changed", "activity_id": quick.id, "version": version},
        ),
    ]


# --- Hide results (L4) ------------------------------------------------------


def test_hide_results(teacher_client, quick):
    response = teacher_client.patch(activity_url(quick), {"hide_results": True}, format="json")

    assert response.status_code == 200
    body = response.json()
    assert body["activity"]["hide_results"] is True
    assert body["activity"]["version"] == 1
    assert "summaries" in body  # the full teacher state
    assert refresh(quick).hide_results is True
    assert teacher_client.get(teacher_state_url(quick)).json()["activity"]["hide_results"] is True

    shown = teacher_client.patch(activity_url(quick), {"hide_results": False}, format="json")
    assert shown.json()["activity"]["hide_results"] is False
    assert refresh(quick).version == 2


def test_setting_the_same_value_is_a_no_op(teacher_client, quick):
    teacher_client.patch(activity_url(quick), {"hide_results": True}, format="json")

    again = teacher_client.patch(activity_url(quick), {"hide_results": True}, format="json")

    assert again.status_code == 200
    assert refresh(quick).version == 1


def test_hide_results_on_an_ended_activity(teacher_client, quick):
    services.end_activity(quick)

    response = teacher_client.patch(activity_url(quick), {"hide_results": True}, format="json")

    assert response.status_code == 200
    assert refresh(quick).hide_results is True
    assert refresh(quick).status == "ENDED"


@pytest.mark.parametrize("data", [{}, {"hide_results": "maybe"}, {"hide_results": None}])
def test_hide_results_validates_input(teacher_client, quick, data):
    response = teacher_client.patch(activity_url(quick), data, format="json")

    assert response.status_code == 400
    assert "hide_results" in response.json()["fields"]
    assert refresh(quick).version == 0


def test_patch_ignores_other_fields(teacher_client, quick):
    response = teacher_client.patch(
        activity_url(quick),
        {"hide_results": True, "status": "ENDED", "show_feedback": True},
        format="json",
    )

    assert response.status_code == 200
    activity = refresh(quick)
    assert activity.is_live
    assert activity.show_feedback is False


def test_hide_results_needs_the_owner(api_client, other_client, quick):
    assert api_client.patch(activity_url(quick), {"hide_results": True}, format="json").status_code == 401
    response = other_client.patch(activity_url(quick), {"hide_results": True}, format="json")
    assert response.status_code == 404
    assert refresh(quick).hide_results is False


def test_hide_results_notifies_only_the_teacher(
    teacher_client, room, quick, sent, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        teacher_client.patch(activity_url(quick), {"hide_results": True}, format="json")

    assert payloads(sent) == [
        (
            f"teacher.room.{room.id}",
            {"type": "activity_updated", "activity_id": quick.id, "version": 1},
        )
    ]


def test_students_never_see_hide_results(quick):
    student = join()
    Activity.objects.filter(pk=quick.pk).update(hide_results=True)

    assert "hide_results" not in student.get(STATE).json()["activity"]
