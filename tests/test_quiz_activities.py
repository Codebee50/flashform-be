"""Teacher-paced quiz activities: launching snapshots the quiz, navigate moves everyone
and locks the question being left, students only ever see the current question and never
see answers early (PRD §5.5 A1, A4, §7, §8, §9, §17)."""

import json
from unittest import mock

import pytest

from activities import services
from activities.models import Activity, ActivityMode, ActivityStatus, Response
from quizzes.models import Quiz
from tests.test_activities import (  # noqa: F401 (fixtures)
    ANSWER_KEYS,
    MC_QUESTION,
    STATE,
    TF_QUESTION,
    activities_url,
    end_url,
    join,
    keys_in,
    launch,
    other_client,
    other_teacher,
    question,
    refresh,
    response_url,
    room,
    teacher_state_url,
)

pytestmark = pytest.mark.django_db

QUIZZES = "/api/quizzes"

# Every string that would reveal an answer, or a question other than the current one.
QUIZ_QUESTIONS = [
    {
        "type": "MC",
        "prompt": "PROMPT-ONE: capital of France?",
        "choices": ["Berlin", "Paris", "Rome"],
        "correct_index": 1,
        "explanation": "SECRET-MC-EXPLANATION",
    },
    {
        "type": "TF",
        "prompt": "PROMPT-TWO: the sky is green.",
        "correct_index": 1,
        "explanation": "SECRET-TF-EXPLANATION",
    },
    {
        "type": "SA",
        "prompt": "PROMPT-THREE: largest French city?",
        "accepted_answers": ["Paris", "SECRET-SA-ANSWER"],
        "explanation": "SECRET-SA-EXPLANATION",
    },
]
PROMPTS = [q["prompt"] for q in QUIZ_QUESTIONS]
SECRETS = [
    "SECRET-MC-EXPLANATION",
    "SECRET-TF-EXPLANATION",
    "SECRET-SA-EXPLANATION",
    "SECRET-SA-ANSWER",
]


def navigate_url(activity) -> str:
    return f"/api/activities/{activity.id}/navigate"


def quiz_url(quiz_id) -> str:
    return f"{QUIZZES}/{quiz_id}"


@pytest.fixture
def quiz(teacher_client) -> Quiz:
    response = teacher_client.post(
        QUIZZES, {"title": "Week 1", "questions": QUIZ_QUESTIONS}, format="json"
    )
    assert response.status_code == 201, response.json()
    return Quiz.objects.get(pk=response.json()["id"])


def start_quiz(client, room, quiz, **options):
    data = {"type": "QUIZ", "quiz_id": quiz.id, "mode": "TEACHER_PACED", **options}
    return client.post(activities_url(room), data, format="json")


@pytest.fixture
def activity(teacher_client, room, quiz) -> Activity:
    response = start_quiz(teacher_client, room, quiz)
    assert response.status_code == 201, response.json()
    return Activity.objects.get(pk=response.json()["activity"]["id"])


@pytest.fixture
def sent():
    """Events handed to the channel layer, as (group, payload)."""
    with mock.patch("activities.broadcast.send") as send:
        yield send


def events(send_mock) -> list[tuple[str, dict]]:
    return [call.args for call in send_mock.call_args_list]


def visible_orders(student) -> list[int]:
    return [q["order"] for q in student.get(STATE).json()["questions"]]


# --- Launching --------------------------------------------------------------


def test_start_quiz_snapshots_its_questions(teacher_client, room, quiz):
    response = start_quiz(teacher_client, room, quiz, show_feedback=True)

    assert response.status_code == 201, response.json()
    body = response.json()
    assert body["activity"] | {"id": None, "started_at": None} == {
        "id": None,
        "room_id": room.id,
        "type": "QUIZ",
        "mode": "TEACHER_PACED",
        "status": "LIVE",
        "quiz_title": "Week 1",
        "source_quiz_id": quiz.id,
        "current_index": 0,
        "show_feedback": True,
        "shuffle_questions": False,
        "hide_results": False,
        "version": 0,
        "started_at": None,
        "ended_at": None,
    }
    assert [{k: v for k, v in q.items() if k != "id"} for q in body["questions"]] == [
        {
            "order": 0,
            "type": "MC",
            "prompt": PROMPTS[0],
            "explanation": "SECRET-MC-EXPLANATION",
            "choices": ["Berlin", "Paris", "Rome"],
            "correct_index": 1,
            "accepted_answers": [],
        },
        {
            "order": 1,
            "type": "TF",
            "prompt": PROMPTS[1],
            "explanation": "SECRET-TF-EXPLANATION",
            "choices": ["True", "False"],
            "correct_index": 1,
            "accepted_answers": [],
        },
        {
            "order": 2,
            "type": "SA",
            "prompt": PROMPTS[2],
            "explanation": "SECRET-SA-EXPLANATION",
            "choices": [],
            "correct_index": None,
            "accepted_answers": ["Paris", "SECRET-SA-ANSWER"],
        },
    ]
    activity = Activity.objects.get(pk=body["activity"]["id"])
    assert [q.id for q in activity.questions.all()] == [q["id"] for q in body["questions"]]
    assert [s["correct_count"] for s in body["summaries"]] == [0, 0, 0]


def test_start_quiz_ends_the_live_activity_and_notifies_everyone(
    teacher_client, room, quiz, sent, django_capture_on_commit_callbacks
):
    quick = services.start_quick_activity(room, type="MC")

    with django_capture_on_commit_callbacks(execute=True):
        response = start_quiz(teacher_client, room, quiz)

    assert response.status_code == 201
    new_id = response.json()["activity"]["id"]
    assert refresh(quick).status == ActivityStatus.ENDED
    old_version = refresh(quick).version
    assert events(sent) == [
        ("room.BIO3AB", {"type": "activity_ended", "activity_id": quick.id, "version": old_version}),
        (f"teacher.room.{room.id}", {"type": "activity_updated", "activity_id": quick.id, "version": old_version}),
        ("room.BIO3AB", {"type": "activity_started", "activity_id": new_id, "version": 0}),
        (f"teacher.room.{room.id}", {"type": "activity_updated", "activity_id": new_id, "version": 0}),
    ]


@pytest.mark.parametrize("whose", ["other_teachers", "missing"])
def test_start_quiz_404s_for_a_quiz_the_teacher_does_not_own(
    teacher_client, other_teacher, room, whose
):
    other_quiz = Quiz.objects.create(owner=other_teacher, title="Not yours")
    other_quiz.questions.create(order=0, type="TF", prompt="?")
    quiz_id = other_quiz.id if whose == "other_teachers" else 999_999
    quick = services.start_quick_activity(room, type="MC")

    response = teacher_client.post(
        activities_url(room),
        {"type": "QUIZ", "quiz_id": quiz_id, "mode": "TEACHER_PACED"},
        format="json",
    )

    assert response.status_code == 404
    assert response.json()["code"] == "quiz_not_found"
    # Nothing happened: the live activity keeps running.
    assert refresh(quick).status == ActivityStatus.LIVE
    assert Activity.objects.count() == 1


def test_another_teacher_cannot_start_a_quiz_in_my_room(other_client, room, quiz):
    response = start_quiz(other_client, room, quiz)

    assert response.status_code == 404
    assert not Activity.objects.exists()


def test_start_quiz_with_no_questions_is_rejected(teacher_client, teacher, room):
    empty = Quiz.objects.create(owner=teacher, title="Empty")

    response = start_quiz(teacher_client, room, empty)

    assert response.status_code == 400
    assert response.json()["code"] == "quiz_empty"
    assert not Activity.objects.exists()


@pytest.mark.parametrize(
    "extra, field",
    [
        ({"quiz_id": None}, "quiz_id"),
        ({"mode": None}, "mode"),
        ({"mode": "SIDEWAYS"}, "mode"),
        ({"mode": "STUDENT_PACED"}, "mode"),
        ({"shuffle_questions": True}, "shuffle_questions"),
    ],
)
def test_start_quiz_validates_input(teacher_client, room, quiz, extra, field):
    data = {"type": "QUIZ", "quiz_id": quiz.id, "mode": "TEACHER_PACED"}
    data.update(extra)
    data = {key: value for key, value in data.items() if value is not None}

    response = teacher_client.post(activities_url(room), data, format="json")

    assert response.status_code == 400
    assert field in response.json()["fields"]
    assert not Activity.objects.exists()


# --- Snapshot survives quiz changes -----------------------------------------


def test_editing_the_quiz_does_not_change_a_launched_activity(teacher_client, quiz, activity):
    before = teacher_client.get(teacher_state_url(activity)).json()
    student = join()

    edited = teacher_client.put(
        quiz_url(quiz.id),
        {
            "title": "Renamed",
            "questions": [
                {"type": "MC", "prompt": "New?", "choices": ["Paris", "Berlin"], "correct_index": 1},
            ],
        },
        format="json",
    )
    assert edited.status_code == 200

    after = teacher_client.get(teacher_state_url(activity)).json()
    assert after["activity"]["quiz_title"] == "Week 1"
    assert after["questions"] == before["questions"]
    # Graded against the snapshot (Paris = 1), not the edited quiz (Paris = 0).
    answer = student.put(response_url(question(activity, 0)), {"choice_index": 1}, format="json")
    assert answer.status_code == 200
    assert Response.objects.get().is_correct is True
    assert student.get(STATE).json()["questions"][0]["prompt"] == PROMPTS[0]


def test_deleting_the_quiz_keeps_the_activity_and_its_answers(teacher_client, quiz, activity):
    student = join()
    student.put(response_url(question(activity, 0)), {"choice_index": 1}, format="json")
    before = teacher_client.get(teacher_state_url(activity)).json()

    assert teacher_client.delete(quiz_url(quiz.id)).status_code == 204

    after = teacher_client.get(teacher_state_url(activity)).json()
    assert after["activity"]["source_quiz_id"] is None
    assert after["activity"]["quiz_title"] == "Week 1"
    assert after["questions"] == before["questions"]
    assert after["responses"] == before["responses"]
    # The run carries on without its quiz.
    assert teacher_client.post(navigate_url(activity), {"index": 1}, format="json").status_code == 200
    assert student.put(response_url(question(activity, 1)), {"choice_index": 0}, format="json").status_code == 200
    assert teacher_client.post(end_url(activity)).status_code == 200


# --- Navigate ---------------------------------------------------------------


def test_navigate_moves_everyone_and_locks_the_question_left(teacher_client, activity):
    answered = join(name="Answered", ip="10.0.0.1")
    silent = join(name="Silent", ip="10.0.0.2")
    answered.put(response_url(question(activity, 0)), {"choice_index": 1}, format="json")
    version = refresh(activity).version

    response = teacher_client.post(navigate_url(activity), {"index": 1}, format="json")

    assert response.status_code == 200, response.json()
    assert response.json()["activity"]["current_index"] == 1
    assert response.json()["activity"]["version"] == version + 1
    assert Response.objects.get().is_locked
    for student in (answered, silent):
        state = student.get(STATE).json()
        assert [q["order"] for q in state["questions"]] == [1]
        assert state["activity"]["current_index"] == 1
        assert state["question_count"] == 3
    # The question left is closed to everyone, answered or not.
    for student in (answered, silent):
        late = student.put(response_url(question(activity, 0)), {"choice_index": 0}, format="json")
        assert late.status_code == 409
        assert late.json()["code"] == "not_current_question"
    # The new question is open.
    now = silent.put(response_url(question(activity, 1)), {"choice_index": 0}, format="json")
    assert now.status_code == 200
    assert now.json()["is_locked"] is False


def test_going_back_keeps_given_answers_locked_but_accepts_new_ones(teacher_client, activity):
    answered = join(name="Answered", ip="10.0.0.1")
    silent = join(name="Silent", ip="10.0.0.2")
    answered.put(response_url(question(activity, 0)), {"choice_index": 0}, format="json")
    teacher_client.post(navigate_url(activity), {"index": 1}, format="json")
    answered.put(response_url(question(activity, 1)), {"choice_index": 1}, format="json")

    response = teacher_client.post(navigate_url(activity), {"index": 0}, format="json")

    assert response.status_code == 200
    assert visible_orders(answered) == [0]
    changed = answered.put(response_url(question(activity, 0)), {"choice_index": 1}, format="json")
    assert changed.status_code == 409
    assert changed.json()["code"] == "response_locked"
    assert silent.put(response_url(question(activity, 0)), {"choice_index": 1}, format="json").status_code == 200
    # Leaving question 1 locked the answer given there too.
    assert Response.objects.get(question=question(activity, 1)).is_locked
    # ...but not the one just given to the current question.
    assert not Response.objects.get(question=question(activity, 0), participant_id=silent.participant_id).is_locked


def test_navigate_can_jump_to_any_question(teacher_client, activity):
    response = teacher_client.post(navigate_url(activity), {"index": 2}, format="json")

    assert response.status_code == 200
    assert refresh(activity).current_index == 2


def test_navigate_to_the_current_question_is_a_no_op(
    teacher_client, activity, sent, django_capture_on_commit_callbacks
):
    student = join()
    student.put(response_url(question(activity, 0)), {"choice_index": 1}, format="json")
    version = refresh(activity).version
    sent.reset_mock()

    with django_capture_on_commit_callbacks(execute=True):
        response = teacher_client.post(navigate_url(activity), {"index": 0}, format="json")

    assert response.status_code == 200
    assert response.json()["activity"]["version"] == version
    assert not Response.objects.get().is_locked
    assert events(sent) == []


def test_navigate_notifies_students_and_teacher_after_commit(
    teacher_client, room, activity, sent, django_capture_on_commit_callbacks
):
    sent.reset_mock()

    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        teacher_client.post(navigate_url(activity), {"index": 1}, format="json")
        assert events(sent) == []  # nothing before commit

    version = refresh(activity).version
    assert len(callbacks) == 2
    assert events(sent) == [
        ("room.BIO3AB", {"type": "activity_updated", "activity_id": activity.id, "version": version}),
        (f"teacher.room.{room.id}", {"type": "activity_updated", "activity_id": activity.id, "version": version}),
    ]


@pytest.mark.parametrize(
    "data, code",
    [
        ({"index": 3}, "invalid_index"),
        ({"index": -1}, "validation_error"),
        ({"index": "next"}, "validation_error"),
        ({}, "validation_error"),
    ],
)
def test_navigate_rejects_bad_indexes(teacher_client, activity, data, code):
    response = teacher_client.post(navigate_url(activity), data, format="json")

    assert response.status_code == 400
    assert response.json()["code"] == code
    assert refresh(activity).current_index == 0
    assert refresh(activity).version == 0


def test_navigate_an_ended_activity_is_a_conflict(teacher_client, activity):
    teacher_client.post(end_url(activity))

    response = teacher_client.post(navigate_url(activity), {"index": 1}, format="json")

    assert response.status_code == 409
    assert response.json()["code"] == "activity_ended"


def test_navigate_a_student_paced_activity_is_a_conflict(teacher_client, room):
    activity = launch(room, [MC_QUESTION, TF_QUESTION], mode=ActivityMode.STUDENT_PACED)

    response = teacher_client.post(navigate_url(activity), {"index": 1}, format="json")

    assert response.status_code == 409
    assert response.json()["code"] == "not_teacher_paced"


def test_navigate_requires_auth_and_ownership(api_client, other_client, activity):
    assert api_client.post(navigate_url(activity), {"index": 1}, format="json").status_code == 401
    response = other_client.post(navigate_url(activity), {"index": 1}, format="json")
    assert response.status_code == 404
    assert refresh(activity).current_index == 0


def test_navigate_after_a_submit_race_never_leaves_an_unlocked_answer(activity):
    """Submit and navigate both lock the activity row, so whichever runs second sees the
    other's result: an answer saved first is locked by navigate; one arriving after is
    rejected."""
    student = join()
    student.put(response_url(question(activity, 0)), {"choice_index": 1}, format="json")
    services.navigate(activity, 1)
    late = student.put(response_url(question(activity, 0)), {"choice_index": 2}, format="json")

    assert late.status_code == 409
    assert Response.objects.get().is_locked
    assert Response.objects.get().choice_index == 1


# --- Ending -----------------------------------------------------------------


def test_ending_a_quiz_locks_everything_and_hides_the_questions(teacher_client, activity):
    student = join()
    student.put(response_url(question(activity, 0)), {"choice_index": 1}, format="json")

    assert teacher_client.post(end_url(activity)).status_code == 200

    state = student.get(STATE).json()
    assert state["activity"]["status"] == "ENDED"
    assert state["questions"] == []
    assert Response.objects.get().is_locked
    late = student.put(response_url(question(activity, 0)), {"choice_index": 0}, format="json")
    assert late.json()["code"] == "activity_ended"


# --- No answer leaks --------------------------------------------------------


def assert_only_current_question(body: dict, index: int, raw: str) -> None:
    assert [q["order"] for q in body["questions"]] == [index]
    for order, prompt in enumerate(PROMPTS):
        assert (prompt in raw) == (order == index), f"prompt {order} visibility wrong"


@pytest.mark.parametrize("show_feedback", [False, True])
def test_students_only_see_the_current_question_and_no_answers(
    teacher_client, room, quiz, show_feedback
):
    response = start_quiz(teacher_client, room, quiz, show_feedback=show_feedback)
    activity = Activity.objects.get(pk=response.json()["activity"]["id"])
    student = join()
    answers = [{"choice_index": 0}, {"choice_index": 1}, {"text_answer": "paris"}]

    for index, answer in enumerate(answers):
        if index:
            teacher_client.post(navigate_url(activity), {"index": index}, format="json")

        # Before answering: only this question, nothing about any answer.
        before = student.get(STATE)
        assert_only_current_question(before.json(), index, before.content.decode())
        assert keys_in(before.json()) & ANSWER_KEYS == set()
        assert not any(secret in before.content.decode() for secret in SECRETS)

        submit = student.put(response_url(question(activity, index)), answer, format="json")
        after = student.get(STATE)
        assert submit.status_code == 200
        assert_only_current_question(after.json(), index, after.content.decode())
        assert keys_in(submit.json()) & ANSWER_KEYS == set()
        assert keys_in(after.json()) & ANSWER_KEYS == set()

        feedback = after.json()["questions"][0]["response"]["feedback"]
        if show_feedback:
            # Locked on submit, so this question's answer (and only this one's) is shown.
            expected = QUIZ_QUESTIONS[index]
            assert feedback["explanation"] == expected["explanation"]
            assert submit.json()["feedback"] == feedback
            others = [s for s in SECRETS if s not in json.dumps(expected)]
            assert not any(secret in after.content.decode() for secret in others)
        else:
            assert feedback is None
            assert submit.json()["feedback"] is None
            assert not any(secret in after.content.decode() for secret in SECRETS)

    # Moving on locks earlier answers, which still reveals nothing without feedback.
    teacher_client.post(navigate_url(activity), {"index": 0}, format="json")
    body = student.get(STATE)
    assert body.json()["questions"][0]["response"]["is_locked"] is True
    if not show_feedback:
        assert body.json()["questions"][0]["response"]["feedback"] is None
        assert not any(secret in body.content.decode() for secret in SECRETS)


def test_teacher_state_is_not_reachable_with_a_participant_token(activity):
    student = join()

    response = student.get(teacher_state_url(activity))

    assert response.status_code == 401
    assert "SECRET" not in response.content.decode()
