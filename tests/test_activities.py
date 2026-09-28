import hashlib
import json

import pytest
from django.db import IntegrityError, transaction
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from activities import services
from activities.models import (
    Activity,
    ActivityMode,
    ActivityQuestion,
    ActivityStatus,
    ActivityType,
    Participant,
    Response,
)
from rooms.models import Room

pytestmark = pytest.mark.django_db

STATE = "/api/participant/state"
LEAVE = "/api/participant/leave"
# Keys that reveal correct answers; they may only appear inside `feedback`.
ANSWER_KEYS = {"correct_index", "accepted_answers", "explanation", "is_correct"}


def activities_url(room) -> str:
    return f"/api/rooms/{room.id}/activities"


def teacher_state_url(activity) -> str:
    return f"/api/activities/{activity.id}/teacher-state"


def end_url(activity) -> str:
    return f"/api/activities/{activity.id}/end"


def join_url(code: str) -> str:
    return f"/api/rooms/{code}/join"


def response_url(question) -> str:
    return f"/api/participant/responses/{question.id}"


# --- Fixtures & helpers -----------------------------------------------------


@pytest.fixture
def other_teacher(django_user_model):
    from accounts.services import mark_email_verified

    user = django_user_model.objects.create_user(
        username="bob@example.com", email="bob@example.com", password="x", first_name="Bob"
    )
    mark_email_verified(user)
    return user


@pytest.fixture
def other_client(other_teacher):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {AccessToken.for_user(other_teacher)}")
    return client


@pytest.fixture
def room(teacher):
    return Room.objects.create(owner=teacher, name="Period 3 Biology", code="BIO3AB")


@pytest.fixture
def quick(room):
    """A LIVE quick MC question (A–D, no correct answer)."""
    return services.start_quick_activity(room, type="MC")


MC_QUESTION = {
    "type": "MC",
    "prompt": "Capital of France?",
    "choices": ["Berlin", "Paris", "Rome"],
    "correct_index": 1,
    "explanation": "SECRET-MC-EXPLANATION",
}
TF_QUESTION = {"type": "TF", "prompt": "The sky is green.", "correct_index": 1, "explanation": "Blue."}
SA_QUESTION = {
    "type": "SA",
    "prompt": "Largest French city?",
    "accepted_answers": ["Paris", "Straße"],
    "explanation": "SECRET-SA-EXPLANATION",
}


def launch(room, questions, *, mode=ActivityMode.STUDENT_PACED, show_feedback=False) -> Activity:
    """An activity with graded questions, as a quiz launch creates, without needing a quiz."""
    return services._launch(
        room, type=ActivityType.QUIZ, mode=mode, questions=questions, show_feedback=show_feedback
    )


def join(code="BIO3AB", name="Grace", ip="10.0.0.1") -> APIClient:
    """Join via the API; returns a client that sends the participant token."""
    response = APIClient().post(join_url(code), {"name": name}, format="json", REMOTE_ADDR=ip)
    assert response.status_code == 201, response.json()
    client = APIClient()
    client.credentials(HTTP_X_PARTICIPANT_TOKEN=response.json()["token"])
    client.participant_id = response.json()["participant_id"]
    return client


def question(activity, order=0) -> ActivityQuestion:
    return activity.questions.get(order=order)


def keys_in(data) -> set[str]:
    """Every dict key anywhere in a JSON value, except inside `feedback` objects."""
    if isinstance(data, dict):
        found = set(data)
        for key, value in data.items():
            if key != "feedback":
                found |= keys_in(value)
        return found
    if isinstance(data, list):
        return set().union(*(keys_in(item) for item in data)) if data else set()
    return set()


def refresh(activity) -> Activity:
    return Activity.objects.get(pk=activity.pk)


# --- Model constraints ------------------------------------------------------


def test_at_most_one_live_activity_per_room(room):
    Activity.objects.create(room=room, type=ActivityType.QUICK)
    Activity.objects.create(room=room, type=ActivityType.QUICK, status=ActivityStatus.ENDED)

    with pytest.raises(IntegrityError), transaction.atomic():
        Activity.objects.create(room=room, type=ActivityType.QUICK)


def test_live_activities_in_different_rooms_are_fine(room, teacher):
    other_room = Room.objects.create(owner=teacher, name="Other", code="OTHER1")
    Activity.objects.create(room=room, type=ActivityType.QUICK)
    Activity.objects.create(room=other_room, type=ActivityType.QUICK)


def test_one_response_per_participant_and_question(quick):
    participant = Participant.objects.create(activity=quick, name="Grace", token_hash="x" * 64)
    Response.objects.create(participant=participant, question=question(quick), choice_index=0)

    with pytest.raises(IntegrityError), transaction.atomic():
        Response.objects.create(participant=participant, question=question(quick), choice_index=1)


# --- Starting and ending ----------------------------------------------------


def test_start_quick_mc_defaults_to_a_to_d(teacher_client, room):
    response = teacher_client.post(
        activities_url(room), {"type": "QUICK", "question": {"type": "MC"}}, format="json"
    )

    assert response.status_code == 201, response.json()
    body = response.json()
    assert body["activity"]["type"] == "QUICK"
    assert body["activity"]["mode"] == "TEACHER_PACED"
    assert body["activity"]["status"] == "LIVE"
    assert body["activity"]["room_id"] == room.id
    assert body["participant_count"] == 0
    [q] = body["questions"]
    assert q["choices"] == ["A", "B", "C", "D"]
    assert q["prompt"] == ""
    assert q["correct_index"] is None
    assert body["summaries"] == [
        {
            "question_id": q["id"],
            "answered_count": 0,
            "correct_count": None,
            "choice_counts": [0, 0, 0, 0],
            "text_counts": [],
        }
    ]


@pytest.mark.parametrize(
    "question_data, choices",
    [
        ({"type": "MC", "prompt": "Pick one", "choices": ["Yes", "No", "Maybe"]}, ["Yes", "No", "Maybe"]),
        ({"type": "TF"}, ["True", "False"]),
        ({"type": "SA", "prompt": "Why?"}, []),
    ],
)
def test_start_quick_question_types(teacher_client, room, question_data, choices):
    response = teacher_client.post(
        activities_url(room), {"type": "QUICK", "question": question_data}, format="json"
    )

    assert response.status_code == 201, response.json()
    assert response.json()["questions"][0]["choices"] == choices
    assert response.json()["questions"][0]["type"] == question_data["type"]


@pytest.mark.parametrize(
    "data, field",
    [
        ({"type": "QUICK", "question": {"type": "TF", "choices": ["a", "b"]}}, "question"),
        ({"type": "QUICK", "question": {"type": "MC", "choices": ["only one"]}}, "question"),
        ({"type": "QUICK", "question": {"type": "MC", "choices": list("ABCDEFG")}}, "question"),
        ({"type": "QUICK", "question": {"type": "MC", "choices": ["A", "  "]}}, "question"),
        ({"type": "QUICK", "question": {"type": "MC", "prompt": "x" * 1001}}, "question"),
        ({"type": "QUICK", "question": {"type": "XX"}}, "question"),
        ({"type": "QUICK"}, "question"),
        ({"type": "QUIZ", "quiz_id": 1}, "mode"),
        ({"type": "QUIZ", "mode": "TEACHER_PACED"}, "quiz_id"),
    ],
)
def test_start_rejects_invalid_input(teacher_client, room, data, field):
    response = teacher_client.post(activities_url(room), data, format="json")

    assert response.status_code == 400
    assert field in response.json()["fields"]
    assert not Activity.objects.exists()


def test_starting_a_new_activity_ends_the_live_one(teacher_client, room, quick):
    student = join()
    student.put(response_url(question(quick)), {"choice_index": 2}, format="json")
    version_before = refresh(quick).version

    response = teacher_client.post(
        activities_url(room), {"type": "QUICK", "question": {"type": "TF"}}, format="json"
    )

    assert response.status_code == 201
    old = refresh(quick)
    assert old.status == ActivityStatus.ENDED
    assert old.ended_at is not None
    assert old.version == version_before + 1
    assert Response.objects.get().is_locked
    new = Activity.objects.get(status=ActivityStatus.LIVE)
    assert new.id == response.json()["activity"]["id"]
    assert Room.objects.get(pk=room.pk).live_activity == new


def test_start_requires_auth_and_ownership(api_client, other_client, room):
    data = {"type": "QUICK", "question": {"type": "MC"}}

    assert api_client.post(activities_url(room), data, format="json").status_code == 401
    response = other_client.post(activities_url(room), data, format="json")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    assert not Activity.objects.exists()


def test_other_teacher_cannot_see_or_end_an_activity(other_client, quick):
    assert other_client.get(teacher_state_url(quick)).status_code == 404
    assert other_client.post(end_url(quick)).status_code == 404
    assert refresh(quick).is_live


def test_end_activity_locks_responses_and_is_idempotent(teacher_client, quick):
    student = join()
    student.put(response_url(question(quick)), {"choice_index": 0}, format="json")

    response = teacher_client.post(end_url(quick))

    assert response.status_code == 200
    assert response.json()["activity"]["status"] == "ENDED"
    assert response.json()["activity"]["ended_at"] is not None
    assert Response.objects.get().is_locked
    version = refresh(quick).version

    again = teacher_client.post(end_url(quick))
    assert again.status_code == 200
    assert refresh(quick).version == version


def test_every_state_change_bumps_the_version(teacher_client, quick):
    assert refresh(quick).version == 0
    student = join()
    assert refresh(quick).version == 1
    student.put(response_url(question(quick)), {"choice_index": 0}, format="json")
    assert refresh(quick).version == 2
    student.post(LEAVE)
    assert refresh(quick).version == 3
    teacher_client.post(end_url(quick))
    assert refresh(quick).version == 4


# --- Join -------------------------------------------------------------------


def test_join_returns_token_once_and_stores_only_its_hash(api_client, quick):
    response = api_client.post(join_url("BIO3AB"), {"name": "  Grace Hopper  "}, format="json")

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"participant_id", "token", "activity_id"}
    assert body["activity_id"] == quick.id
    participant = Participant.objects.get()
    assert str(participant.id) == body["participant_id"]
    assert participant.name == "Grace Hopper"
    assert participant.token_hash == hashlib.sha256(body["token"].encode()).hexdigest()
    assert body["token"] not in participant.token_hash
    assert len(body["token"]) >= 40


def test_join_code_is_case_insensitive_and_ignores_spaces(api_client, quick):
    response = api_client.post(join_url("bio 3ab"), {"name": "Grace"}, format="json")

    assert response.status_code == 201


def test_every_join_gets_its_own_token(quick):
    first, second = join(name="Sam"), join(name="Sam")

    assert first.participant_id != second.participant_id
    assert Participant.objects.count() == 2


@pytest.mark.parametrize(
    "setup, status, code",
    [
        ("missing", 404, "room_not_found"),
        ("locked", 423, "room_locked"),
        ("no_activity", 409, "no_live_activity"),
        ("ended", 409, "no_live_activity"),
    ],
)
def test_join_errors(api_client, room, setup, status, code):
    if setup == "locked":
        Room.objects.filter(pk=room.pk).update(is_locked=True)
        services.start_quick_activity(room, type="MC")
    elif setup == "ended":
        services.end_activity(services.start_quick_activity(room, type="MC"))
    code_to_join = "NOPE99" if setup == "missing" else room.code

    response = api_client.post(join_url(code_to_join), {"name": "Grace"}, format="json")

    assert response.status_code == status
    assert response.json()["code"] == code
    assert not Participant.objects.exists()


@pytest.mark.parametrize("name", ["", "   ", "x" * 41])
def test_join_validates_name(api_client, quick, name):
    response = api_client.post(join_url("BIO3AB"), {"name": name}, format="json")

    assert response.status_code == 400
    assert "name" in response.json()["fields"]


def test_join_is_throttled_per_ip(api_client, quick):
    for i in range(10):
        response = api_client.post(join_url("BIO3AB"), {"name": f"S{i}"}, format="json")
        assert response.status_code == 201

    response = api_client.post(join_url("BIO3AB"), {"name": "One too many"}, format="json")
    assert response.status_code == 429
    assert response.json()["code"] == "throttled"


def test_join_is_throttled_per_room(api_client, quick, teacher):
    for i in range(60):
        response = api_client.post(
            join_url("BIO3AB"), {"name": f"S{i}"}, format="json", REMOTE_ADDR=f"10.0.{i}.1"
        )
        assert response.status_code == 201

    response = api_client.post(
        join_url("bio3ab"), {"name": "Late"}, format="json", REMOTE_ADDR="10.9.9.9"
    )
    assert response.status_code == 429

    # Other rooms have their own budget.
    other = Room.objects.create(owner=teacher, name="Other", code="OTHER1")
    services.start_quick_activity(other, type="TF")
    response = api_client.post(
        join_url("OTHER1"), {"name": "Fine"}, format="json", REMOTE_ADDR="10.9.9.9"
    )
    assert response.status_code == 201


# --- Participant token ------------------------------------------------------


@pytest.mark.parametrize("headers", [{}, {"HTTP_X_PARTICIPANT_TOKEN": "not-a-real-token"}])
def test_student_endpoints_need_a_valid_token(api_client, quick, headers):
    for response in [
        api_client.get(STATE, **headers),
        api_client.put(response_url(question(quick)), {"choice_index": 0}, format="json", **headers),
        api_client.post(LEAVE, **headers),
    ]:
        assert response.status_code == 401
        assert response.json()["code"] == "invalid_participant_token"


def test_teacher_jwt_is_not_a_participant_token(teacher_client, quick):
    assert teacher_client.get(STATE).status_code == 401


def test_token_only_works_for_its_own_activity(teacher, quick):
    student = join()
    other_room = Room.objects.create(owner=teacher, name="Other", code="OTHER1")
    elsewhere = services.start_quick_activity(other_room, type="MC")

    response = student.put(response_url(question(elsewhere)), {"choice_index": 0}, format="json")

    assert response.status_code == 404
    assert response.json()["code"] == "question_not_found"
    assert not Response.objects.exists()


def test_token_from_an_ended_activity_cannot_answer_the_next_one(room, quick):
    student = join()
    next_activity = services.start_quick_activity(room, type="TF")

    new_question = student.put(response_url(question(next_activity)), {"choice_index": 0}, format="json")
    old_question = student.put(response_url(question(quick)), {"choice_index": 0}, format="json")

    assert new_question.status_code == 404
    assert old_question.status_code == 409
    assert old_question.json()["code"] == "activity_ended"
    state = student.get(STATE).json()
    assert state["activity"]["id"] == quick.id
    assert state["activity"]["status"] == "ENDED"
    assert state["questions"] == []


def test_leave_invalidates_the_token_but_keeps_answers(quick):
    student = join()
    student.put(response_url(question(quick)), {"choice_index": 1}, format="json")

    assert student.post(LEAVE).status_code == 204

    assert student.get(STATE).status_code == 401
    assert Participant.objects.get().is_removed
    assert Response.objects.count() == 1


# --- Submitting -------------------------------------------------------------


def test_participant_state_for_a_quick_question(quick):
    student = join(name="Grace")

    body = student.get(STATE).json()

    assert body["activity"] == {
        "id": quick.id,
        "type": "QUICK",
        "mode": "TEACHER_PACED",
        "status": "LIVE",
        "quiz_title": "",
        "current_index": 0,
        "show_feedback": False,
        "version": 1,
    }
    assert body["participant"]["name"] == "Grace"
    assert body["question_count"] == 1
    assert body["questions"] == [
        {
            "id": question(quick).id,
            "order": 0,
            "type": "MC",
            "prompt": "",
            "choices": ["A", "B", "C", "D"],
            "response": None,
        }
    ]


def test_submitting_twice_upserts_one_row_and_the_second_value_wins(quick):
    student = join()
    url = response_url(question(quick))

    first = student.put(url, {"choice_index": 0}, format="json")
    second = student.put(url, {"choice_index": 3}, format="json")

    assert first.status_code == second.status_code == 200
    assert Response.objects.count() == 1
    assert Response.objects.get().choice_index == 3
    body = second.json()
    assert body["choice_index"] == 3
    assert body["is_locked"] is False
    assert body["feedback"] is None
    assert student.get(STATE).json()["questions"][0]["response"]["choice_index"] == 3


def test_retrying_the_same_answer_is_harmless(quick):
    student = join()
    for _ in range(3):
        assert student.put(response_url(question(quick)), {"choice_index": 1}, format="json").status_code == 200

    assert Response.objects.count() == 1


def test_locked_response_cannot_change(quick):
    student = join()
    url = response_url(question(quick))
    student.put(url, {"choice_index": 0}, format="json")
    Response.objects.update(is_locked=True)

    response = student.put(url, {"choice_index": 1}, format="json")

    assert response.status_code == 409
    assert response.json()["code"] == "response_locked"
    assert Response.objects.get().choice_index == 0


def test_with_feedback_on_a_response_locks_immediately(room):
    activity = launch(room, [MC_QUESTION], show_feedback=True)
    student = join()
    url = response_url(question(activity))

    first = student.put(url, {"choice_index": 0}, format="json")
    second = student.put(url, {"choice_index": 1}, format="json")

    assert first.json()["is_locked"] is True
    assert second.status_code == 409
    assert second.json()["code"] == "response_locked"


def test_teacher_paced_only_accepts_the_current_question(room):
    activity = launch(room, [MC_QUESTION, TF_QUESTION], mode=ActivityMode.TEACHER_PACED)
    student = join()

    response = student.put(response_url(question(activity, 1)), {"choice_index": 0}, format="json")

    assert response.status_code == 409
    assert response.json()["code"] == "not_current_question"
    # Only the current question is visible.
    assert [q["order"] for q in student.get(STATE).json()["questions"]] == [0]


def test_student_paced_rejects_answers_after_finish(room):
    activity = launch(room, [MC_QUESTION, TF_QUESTION])
    student = join()
    Participant.objects.update(finished_at="2026-01-01T00:00:00Z")

    response = student.put(response_url(question(activity)), {"choice_index": 0}, format="json")

    assert response.status_code == 409
    assert response.json()["code"] == "already_finished"


def test_ended_activity_rejects_answers(quick):
    student = join()
    services.end_activity(quick)

    response = student.put(response_url(question(quick)), {"choice_index": 0}, format="json")

    assert response.status_code == 409
    assert response.json()["code"] == "activity_ended"


@pytest.mark.parametrize(
    "question_data, payload",
    [
        (MC_QUESTION, {"choice_index": 3}),  # only 3 choices
        (MC_QUESTION, {"text_answer": "Paris"}),
        (MC_QUESTION, {}),
        (MC_QUESTION, {"choice_index": 0, "text_answer": "Paris"}),
        (MC_QUESTION, {"choice_index": -1}),
        (TF_QUESTION, {"choice_index": 2}),
        (SA_QUESTION, {"choice_index": 0}),
        (SA_QUESTION, {"text_answer": "   "}),
        (SA_QUESTION, {"text_answer": "x" * 501}),
    ],
)
def test_answer_must_fit_the_question(room, question_data, payload):
    activity = launch(room, [question_data])
    student = join()

    response = student.put(response_url(question(activity)), payload, format="json")

    assert response.status_code == 400
    assert not Response.objects.exists()


# --- Grading ----------------------------------------------------------------


@pytest.mark.parametrize(
    "question_data, choice_index, text_answer, expected",
    [
        (MC_QUESTION, 1, "", True),
        (MC_QUESTION, 0, "", False),
        ({**MC_QUESTION, "correct_index": None}, 1, "", None),
        (TF_QUESTION, 1, "", True),
        (TF_QUESTION, 0, "", False),
        ({"type": "TF", "prompt": "Opinion?"}, 0, "", None),
        (SA_QUESTION, None, "Paris", True),
        (SA_QUESTION, None, "  pARIS \n", True),
        (SA_QUESTION, None, "STRASSE", True),  # casefold, not lower
        (SA_QUESTION, None, "Lyon", False),
        (SA_QUESTION, None, "Par is", False),  # no fuzzy matching
        ({"type": "SA", "prompt": "Thoughts?"}, None, "anything", None),
    ],
)
def test_grade(room, question_data, choice_index, text_answer, expected):
    activity = launch(room, [question_data])

    assert services.grade(question(activity), choice_index, text_answer) is expected


def test_grading_is_stored_on_the_response(room):
    activity = launch(room, [MC_QUESTION, TF_QUESTION, SA_QUESTION])
    student = join()

    student.put(response_url(question(activity, 0)), {"choice_index": 1}, format="json")
    student.put(response_url(question(activity, 1)), {"choice_index": 0}, format="json")
    student.put(response_url(question(activity, 2)), {"text_answer": " paris "}, format="json")

    results = {r.question.order: r for r in Response.objects.select_related("question")}
    assert results[0].is_correct is True
    assert results[1].is_correct is False
    assert results[2].is_correct is True
    assert results[2].text_answer == "paris"


# --- No correct-answer leaks ------------------------------------------------


@pytest.mark.parametrize("show_feedback", [False, True])
def test_state_never_leaks_answers_before_the_response_is_locked(room, show_feedback):
    activity = launch(room, [MC_QUESTION, SA_QUESTION], show_feedback=show_feedback)
    student = join()

    before = student.get(STATE)
    assert keys_in(before.json()) & ANSWER_KEYS == set()
    for secret in ["SECRET-MC-EXPLANATION", "SECRET-SA-EXPLANATION", "Straße"]:
        assert secret not in before.content.decode()

    # Answer only the MC question.
    submit = student.put(response_url(question(activity, 0)), {"choice_index": 0}, format="json")
    after = student.get(STATE)

    assert keys_in(submit.json()) & ANSWER_KEYS == set()
    assert keys_in(after.json()) & ANSWER_KEYS == set()
    # The unanswered SA question never reveals its answers.
    assert "SECRET-SA-EXPLANATION" not in after.content.decode()
    assert "Straße" not in after.content.decode()
    mc, sa = after.json()["questions"]
    assert sa["response"] is None
    if show_feedback:
        assert mc["response"]["feedback"] == {
            "is_correct": False,
            "correct_index": 1,
            "accepted_answers": [],
            "explanation": "SECRET-MC-EXPLANATION",
        }
        assert submit.json()["feedback"] == mc["response"]["feedback"]
    else:
        assert mc["response"]["feedback"] is None
        assert submit.json()["feedback"] is None
        assert "SECRET-MC-EXPLANATION" not in after.content.decode()


def test_locked_response_without_feedback_setting_reveals_nothing(room):
    activity = launch(room, [MC_QUESTION], mode=ActivityMode.TEACHER_PACED)
    student = join()
    student.put(response_url(question(activity)), {"choice_index": 1}, format="json")
    Response.objects.update(is_locked=True)  # e.g. the teacher moved on

    body = student.get(STATE).json()

    assert body["questions"][0]["response"]["is_locked"] is True
    assert body["questions"][0]["response"]["feedback"] is None
    assert "SECRET-MC-EXPLANATION" not in json.dumps(body)


def test_ended_activity_shows_no_questions(room):
    activity = launch(room, [MC_QUESTION], show_feedback=True)
    student = join()
    services.end_activity(activity)

    body = student.get(STATE).json()

    assert body["activity"]["status"] == "ENDED"
    assert body["questions"] == []
    assert body["question_count"] == 1


# --- Teacher state ----------------------------------------------------------


def test_teacher_state_summarizes_responses(teacher_client, room):
    activity = launch(room, [MC_QUESTION, SA_QUESTION])
    answers = [(0, "Paris"), (1, " paris "), (1, "Lyon"), (1, "lyon")]
    for i, (choice, text) in enumerate(answers):
        student = join(name=f"S{i}", ip=f"10.0.0.{i}")
        student.put(response_url(question(activity, 0)), {"choice_index": choice}, format="json")
        student.put(response_url(question(activity, 1)), {"text_answer": text}, format="json")
    # A student who left is not counted.
    quitter = join(name="Quitter", ip="10.0.1.1")
    quitter.put(response_url(question(activity, 0)), {"choice_index": 2}, format="json")
    quitter.post(LEAVE)

    body = teacher_client.get(teacher_state_url(activity)).json()

    assert body["participant_count"] == 4
    assert sorted(p["name"] for p in body["participants"]) == ["S0", "S1", "S2", "S3"]
    assert len(body["responses"]) == 8
    assert body["questions"][0]["correct_index"] == 1
    assert body["questions"][1]["accepted_answers"] == ["Paris", "Straße"]
    mc, sa = body["summaries"]
    assert mc["answered_count"] == 4
    assert mc["correct_count"] == 3
    assert mc["choice_counts"] == [1, 3, 0]
    assert sa["correct_count"] == 2
    assert sa["choice_counts"] == []
    assert sorted((t["answer"].casefold(), t["count"]) for t in sa["text_counts"]) == [
        ("lyon", 2),
        ("paris", 2),
    ]


def test_teacher_state_query_count_does_not_grow_with_students(
    teacher, teacher_client, quick, django_assert_max_num_queries
):
    for i in range(5):
        join(name=f"S{i}", ip=f"10.0.0.{i}").put(
            response_url(question(quick)), {"choice_index": i % 4}, format="json"
        )

    with django_assert_max_num_queries(6):
        response = teacher_client.get(teacher_state_url(quick))
    assert response.json()["summaries"][0]["choice_counts"] == [2, 1, 1, 1]


# --- Rooms integration ------------------------------------------------------


def test_rooms_show_the_live_activity(teacher_client, api_client, room, quick):
    listed = teacher_client.get("/api/rooms").json()[0]["live_activity"]
    public = api_client.get("/api/rooms/BIO3AB/public").json()

    assert listed["id"] == quick.id
    assert listed["type"] == "QUICK"
    assert public["live_activity_id"] == quick.id

    services.end_activity(quick)
    assert teacher_client.get("/api/rooms").json()[0]["live_activity"] is None
