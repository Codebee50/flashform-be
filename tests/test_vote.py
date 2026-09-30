"""Start Vote: a LIVE short answer quick question becomes a quick MC question whose options
are its distinct answers, trimmed and case-insensitive (PRD §5.4 QQ4, §17)."""

import pytest

from activities import services
from activities.models import Activity, ActivityStatus, ActivityType, Response
from tests.test_activities import (  # noqa: F401 (fixtures)
    MC_QUESTION,
    SA_QUESTION,
    STATE,
    join,
    launch,
    other_client,
    other_teacher,
    question,
    refresh,
    response_url,
    room,
)
from tests.test_quiz_activities import events, sent  # noqa: F401 (fixtures)

pytestmark = pytest.mark.django_db


def vote_url(activity) -> str:
    return f"/api/activities/{activity.id}/vote"


@pytest.fixture
def short_answer(room) -> Activity:
    return services.start_quick_activity(room, type="SA", prompt="Name a primary colour")


def answer_all(activity, answers: list[str]) -> list:
    """One student per answer, each from their own IP (join throttle)."""
    students = []
    for i, text in enumerate(answers):
        student = join(name=f"S{i}", ip=f"10.0.0.{i + 1}")
        response = student.put(response_url(question(activity)), {"text_answer": text}, format="json")
        assert response.status_code == 200, response.json()
        students.append(student)
    return students


def test_vote_turns_distinct_answers_into_mc_options(teacher_client, short_answer):
    answer_all(short_answer, ["Red", "  blue ", "RED", "Yellow", "red", "Blue"])

    response = teacher_client.post(vote_url(short_answer))

    assert response.status_code == 201, response.json()
    body = response.json()
    assert body["activity"]["type"] == "QUICK"
    assert body["activity"]["mode"] == "TEACHER_PACED"
    assert body["activity"]["status"] == "LIVE"
    assert body["activity"]["id"] != short_answer.id
    [vote_question] = body["questions"]
    # Grouped trimmed and case-insensitively; spelled as first submitted, in submission order.
    assert vote_question["type"] == "MC"
    assert vote_question["prompt"] == "Name a primary colour"
    assert vote_question["choices"] == ["Red", "blue", "Yellow"]
    assert vote_question["correct_index"] is None
    assert body["summaries"][0]["correct_count"] is None
    assert body["participants"] == []

    ended = refresh(short_answer)
    assert ended.status == ActivityStatus.ENDED
    assert not Response.objects.filter(question__activity=ended, is_locked=False).exists()


def test_vote_ignores_removed_and_departed_participants(teacher_client, short_answer):
    removed, left, _ = answer_all(short_answer, ["Green", "Purple", "Red"])
    teacher_client.delete(f"/api/activities/{short_answer.id}/participants/{removed.participant_id}")
    left.post("/api/participant/leave")
    join(name="Late", ip="10.0.1.1").put(
        response_url(question(short_answer)), {"text_answer": "Blue"}, format="json"
    )

    response = teacher_client.post(vote_url(short_answer))

    assert response.json()["questions"][0]["choices"] == ["Red", "Blue"]


def test_students_rejoin_and_answer_the_vote(teacher_client, short_answer):
    answer_all(short_answer, ["Red", "Blue"])
    vote_id = teacher_client.post(vote_url(short_answer)).json()["activity"]["id"]
    vote = Activity.objects.get(pk=vote_id)

    student = join(name="Grace", ip="10.0.2.1")
    state = student.get(STATE).json()
    submit = student.put(response_url(question(vote)), {"choice_index": 1}, format="json")

    assert state["activity"]["id"] == vote_id
    assert state["questions"][0]["choices"] == ["Red", "Blue"]
    assert submit.status_code == 200
    summary = teacher_client.get(f"/api/activities/{vote_id}/teacher-state").json()["summaries"]
    assert summary[0]["choice_counts"] == [0, 1]


def test_vote_notifies_everyone_after_commit(
    teacher_client, room, short_answer, sent, django_capture_on_commit_callbacks
):
    answer_all(short_answer, ["Red", "Blue"])
    sent.reset_mock()

    with django_capture_on_commit_callbacks(execute=True):
        response = teacher_client.post(vote_url(short_answer))
        assert events(sent) == []  # nothing before commit

    vote_id = response.json()["activity"]["id"]
    ended_version = refresh(short_answer).version
    assert events(sent) == [
        ("room.BIO3AB", {"type": "activity_ended", "activity_id": short_answer.id, "version": ended_version}),
        (f"teacher.room.{room.id}", {"type": "activity_updated", "activity_id": short_answer.id, "version": ended_version}),
        ("room.BIO3AB", {"type": "activity_started", "activity_id": vote_id, "version": 0}),
        (f"teacher.room.{room.id}", {"type": "activity_updated", "activity_id": vote_id, "version": 0}),
    ]


def test_retrying_a_vote_does_not_start_a_second_one(teacher_client, short_answer):
    answer_all(short_answer, ["Red", "Blue"])
    first = teacher_client.post(vote_url(short_answer))

    retry = teacher_client.post(vote_url(short_answer))

    assert retry.status_code == 409
    assert retry.json()["code"] == "activity_ended"
    live = Activity.objects.get(status=ActivityStatus.LIVE)
    assert live.id == first.json()["activity"]["id"]
    assert Activity.objects.count() == 2


def test_vote_on_an_ended_short_answer_is_a_conflict(teacher_client, short_answer):
    answer_all(short_answer, ["Red", "Blue"])
    teacher_client.post(f"/api/activities/{short_answer.id}/end")

    response = teacher_client.post(vote_url(short_answer))

    assert response.status_code == 409
    assert response.json()["code"] == "activity_ended"
    assert Activity.objects.count() == 1


@pytest.mark.parametrize("kind", ["quick_mc", "quick_tf", "quiz_sa"])
def test_vote_is_only_for_short_answer_quick_questions(teacher_client, room, kind):
    if kind == "quiz_sa":
        activity = launch(room, [SA_QUESTION, MC_QUESTION])
    else:
        activity = services.start_quick_activity(room, type=kind[-2:].upper())

    response = teacher_client.post(vote_url(activity))

    assert response.status_code == 409
    assert response.json()["code"] == "not_short_answer"
    assert refresh(activity).is_live
    assert Activity.objects.count() == 1


@pytest.mark.parametrize("answers", [[], ["Red"], ["Red", " red", "RED "]])
def test_vote_needs_two_distinct_answers(teacher_client, short_answer, answers):
    answer_all(short_answer, answers)
    version = refresh(short_answer).version

    response = teacher_client.post(vote_url(short_answer))

    assert response.status_code == 409
    assert response.json()["code"] == "not_enough_answers"
    # Nothing changed: the short answer question keeps running.
    assert refresh(short_answer).is_live
    assert refresh(short_answer).version == version
    assert Activity.objects.count() == 1


def test_vote_requires_auth_and_ownership(api_client, other_client, short_answer):
    answer_all(short_answer, ["Red", "Blue"])

    assert api_client.post(vote_url(short_answer)).status_code == 401
    missing = other_client.post(vote_url(short_answer))
    assert missing.status_code == 404
    assert refresh(short_answer).is_live
    assert not Activity.objects.filter(type=ActivityType.QUICK).exclude(pk=short_answer.pk).exists()
