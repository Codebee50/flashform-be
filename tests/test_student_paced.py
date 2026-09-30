"""Student-paced quizzes, Finish and the show-feedback option: students see every question
and can change answers until they finish; with feedback on, each answer locks on submit
and only then reveals its correct answer; the teacher sees each student's progress
(PRD §5.5 A1, A2, §5.7 L3, §8, §17)."""

import json

import pytest

from activities import services
from activities.broadcast import get_store
from activities.models import Activity, ActivityMode, Participant, Response
from tests.test_activities import (  # noqa: F401 (fixtures)
    ANSWER_KEYS,
    LEAVE,
    STATE,
    end_url,
    join,
    keys_in,
    launch,
    MC_QUESTION,
    other_teacher,
    question,
    refresh,
    response_url,
    room,
    teacher_state_url,
)
from tests.test_quiz_activities import (  # noqa: F401 (fixtures)
    PROMPTS,
    QUIZ_QUESTIONS,
    SECRETS,
    events,
    navigate_url,
    quiz,
    sent,
    start_quiz,
)

pytestmark = pytest.mark.django_db

FINISH = "/api/participant/finish"

# One correct and one wrong answer per question of QUIZ_QUESTIONS (MC, TF, SA).
RIGHT = [{"choice_index": 1}, {"choice_index": 1}, {"text_answer": " paris "}]
WRONG = [{"choice_index": 0}, {"choice_index": 0}, {"text_answer": "Lyon"}]


def start_student_paced(client, room, quiz, **options) -> Activity:
    response = start_quiz(client, room, quiz, mode="STUDENT_PACED", **options)
    assert response.status_code == 201, response.json()
    return Activity.objects.get(pk=response.json()["activity"]["id"])


@pytest.fixture
def activity(teacher_client, room, quiz) -> Activity:
    return start_student_paced(teacher_client, room, quiz)


@pytest.fixture
def feedback_activity(teacher_client, room, quiz) -> Activity:
    return start_student_paced(teacher_client, room, quiz, show_feedback=True)


def answer(student, activity, index, data):
    return student.put(response_url(question(activity, index)), data, format="json")


def secrets_in(raw: str) -> set[str]:
    return {secret for secret in SECRETS if secret in raw}


def question_secrets(index: int) -> set[str]:
    return secrets_in(json.dumps(QUIZ_QUESTIONS[index]))


# --- Launching --------------------------------------------------------------


def test_start_a_student_paced_quiz(teacher_client, room, quiz):
    response = start_quiz(teacher_client, room, quiz, mode="STUDENT_PACED", show_feedback=True)

    assert response.status_code == 201, response.json()
    body = response.json()["activity"]
    assert body["mode"] == "STUDENT_PACED"
    assert body["show_feedback"] is True
    assert body["shuffle_questions"] is False
    assert len(response.json()["questions"]) == 3


# --- Student state ----------------------------------------------------------


@pytest.mark.parametrize("show_feedback", [False, True])
def test_student_sees_every_question_and_no_answers(teacher_client, room, quiz, show_feedback):
    start_student_paced(teacher_client, room, quiz, show_feedback=show_feedback)
    student = join()

    response = student.get(STATE)

    body = response.json()
    assert body["activity"]["mode"] == "STUDENT_PACED"
    assert body["participant"]["finished_at"] is None
    assert body["question_count"] == 3
    assert [q["order"] for q in body["questions"]] == [0, 1, 2]
    assert [q["prompt"] for q in body["questions"]] == PROMPTS
    assert [q["response"] for q in body["questions"]] == [None, None, None]
    assert keys_in(body) & ANSWER_KEYS == set()
    assert secrets_in(response.content.decode()) == set()


def test_answers_can_be_given_in_any_order_and_changed_until_finish(activity):
    student = join()

    assert answer(student, activity, 2, WRONG[2]).status_code == 200
    assert answer(student, activity, 0, WRONG[0]).status_code == 200
    changed = answer(student, activity, 2, RIGHT[2])

    assert changed.status_code == 200
    assert changed.json()["is_locked"] is False
    assert changed.json()["feedback"] is None
    assert changed.json()["text_answer"] == "paris"
    questions = student.get(STATE).json()["questions"]
    assert questions[0]["response"]["choice_index"] == 0
    assert questions[1]["response"] is None
    assert questions[2]["response"]["text_answer"] == "paris"
    assert Response.objects.filter(is_locked=False).count() == 2


# --- Finish -----------------------------------------------------------------


def test_finish_locks_every_answer_and_rejects_new_ones(activity):
    student = join()
    answer(student, activity, 0, RIGHT[0])
    answer(student, activity, 1, WRONG[1])

    response = student.post(FINISH)

    assert response.status_code == 200, response.json()
    body = response.json()
    assert body == student.get(STATE).json()
    assert body["participant"]["finished_at"] is not None
    # Still shows every question, answers read-only.
    assert [q["order"] for q in body["questions"]] == [0, 1, 2]
    assert [q["response"] and q["response"]["is_locked"] for q in body["questions"]] == [
        True,
        True,
        None,
    ]
    assert not Response.objects.filter(is_locked=False).exists()
    # Neither answered nor unanswered questions accept anything any more.
    for index in range(3):
        late = answer(student, activity, index, RIGHT[index])
        assert late.status_code == 409
        assert late.json()["code"] == "already_finished"
    assert Response.objects.count() == 2
    assert Response.objects.get(question=question(activity, 1)).choice_index == 0


def test_finish_without_feedback_reveals_nothing(activity):
    student = join()
    for index in range(3):
        answer(student, activity, index, WRONG[index])

    response = student.post(FINISH)

    assert keys_in(response.json()) & ANSWER_KEYS == set()
    assert [q["response"]["feedback"] for q in response.json()["questions"]] == [None] * 3
    assert secrets_in(response.content.decode()) == set()
    assert secrets_in(student.get(STATE).content.decode()) == set()


def test_finish_with_feedback_reveals_only_answered_questions(feedback_activity):
    student = join()
    answer(student, feedback_activity, 1, WRONG[1])

    response = student.post(FINISH)

    q0, q1, q2 = response.json()["questions"]
    assert q1["response"]["feedback"] == {
        "is_correct": False,
        "correct_index": 1,
        "accepted_answers": [],
        "explanation": "SECRET-TF-EXPLANATION",
    }
    assert q0["response"] is None and q2["response"] is None
    assert keys_in(response.json()) & ANSWER_KEYS == set()
    assert secrets_in(response.content.decode()) == question_secrets(1)


def test_finish_only_affects_the_student_who_finished(activity):
    finished = join(name="Done", ip="10.0.0.1")
    working = join(name="Working", ip="10.0.0.2")
    answer(finished, activity, 0, RIGHT[0])
    answer(working, activity, 0, WRONG[0])

    finished.post(FINISH)

    assert working.get(STATE).json()["participant"]["finished_at"] is None
    changed = answer(working, activity, 0, RIGHT[0])
    assert changed.status_code == 200
    assert changed.json()["is_locked"] is False
    assert answer(working, activity, 1, RIGHT[1]).status_code == 200


def test_finish_is_idempotent(
    activity, sent, django_capture_on_commit_callbacks
):
    student = join()
    answer(student, activity, 0, RIGHT[0])
    first = student.post(FINISH)
    version = refresh(activity).version
    sent.reset_mock()

    with django_capture_on_commit_callbacks(execute=True):
        again = student.post(FINISH)

    assert again.status_code == 200
    assert again.json() == first.json()
    assert refresh(activity).version == version
    assert events(sent) == []


def test_finish_bumps_the_version_and_notifies_the_teacher_after_commit(
    room, activity, sent, django_capture_on_commit_callbacks
):
    student = join()
    version = refresh(activity).version
    get_store().clear()  # the join just used up the throttle window
    sent.reset_mock()

    with django_capture_on_commit_callbacks(execute=True):
        response = student.post(FINISH)
        assert events(sent) == []  # nothing before commit

    assert response.json()["activity"]["version"] == version + 1
    assert events(sent) == [
        (
            f"teacher.room.{room.id}",
            {"type": "participants_changed", "activity_id": activity.id, "version": version + 1},
        ),
    ]


def test_finish_with_no_answers_is_fine(activity):
    student = join()

    response = student.post(FINISH)

    assert response.status_code == 200
    assert response.json()["participant"]["finished_at"] is not None
    assert not Response.objects.exists()


@pytest.mark.parametrize("mode", [ActivityMode.TEACHER_PACED, "QUICK"])
def test_finish_is_only_for_student_paced_activities(room, mode):
    if mode == "QUICK":
        services.start_quick_activity(room, type="TF")
    else:
        launch(room, [MC_QUESTION], mode=mode)
    student = join()
    answer(student, Activity.objects.get(), 0, {"choice_index": 0})

    response = student.post(FINISH)

    assert response.status_code == 409
    assert response.json()["code"] == "not_student_paced"
    assert Participant.objects.get().finished_at is None
    assert not Response.objects.get().is_locked


def test_finish_an_ended_activity_is_a_conflict(teacher_client, activity):
    student = join()
    teacher_client.post(end_url(activity))

    response = student.post(FINISH)

    assert response.status_code == 409
    assert response.json()["code"] == "activity_ended"
    assert Participant.objects.get().finished_at is None


@pytest.mark.parametrize("headers", [{}, {"HTTP_X_PARTICIPANT_TOKEN": "not-a-real-token"}])
def test_finish_needs_a_valid_token(api_client, activity, headers):
    response = api_client.post(FINISH, **headers)

    assert response.status_code == 401
    assert response.json()["code"] == "invalid_participant_token"


def test_finish_after_leaving_is_unauthorized(activity):
    student = join()
    student.post(LEAVE)

    response = student.post(FINISH)

    assert response.status_code == 401
    assert Participant.objects.get().finished_at is None


# --- Show feedback ----------------------------------------------------------


@pytest.mark.parametrize("index", [0, 1, 2])
@pytest.mark.parametrize("answers", [RIGHT, WRONG], ids=["right", "wrong"])
def test_feedback_locks_on_submit_and_reveals_only_that_question(feedback_activity, index, answers):
    student = join()
    expected = QUIZ_QUESTIONS[index]

    submit = answer(student, feedback_activity, index, answers[index])

    assert submit.status_code == 200
    assert submit.json()["is_locked"] is True
    assert submit.json()["feedback"] == {
        "is_correct": answers is RIGHT,
        "correct_index": expected.get("correct_index"),
        "accepted_answers": expected.get("accepted_answers", []),
        "explanation": expected["explanation"],
    }
    assert secrets_in(submit.content.decode()) == question_secrets(index)

    state = student.get(STATE)
    assert keys_in(state.json()) & ANSWER_KEYS == set()
    # Only this question carries feedback; the others leak nothing.
    for view in state.json()["questions"]:
        if view["order"] == index:
            assert view["response"]["feedback"] == submit.json()["feedback"]
        else:
            assert view["response"] is None
    assert secrets_in(state.content.decode()) == question_secrets(index)

    # Locked: the answer can't change.
    other = WRONG if answers is RIGHT else RIGHT
    retry = answer(student, feedback_activity, index, other[index])
    assert retry.status_code == 409
    assert retry.json()["code"] == "response_locked"
    assert Response.objects.get().is_correct is (answers is RIGHT)


def test_feedback_accumulates_as_questions_are_answered(feedback_activity):
    student = join()

    for index in range(3):
        before = student.get(STATE)
        # Nothing about questions not yet answered.
        assert secrets_in(before.content.decode()) == set().union(
            *(question_secrets(i) for i in range(index))
        )
        answer(student, feedback_activity, index, RIGHT[index])

    after = student.get(STATE).json()
    assert [q["response"]["feedback"]["is_correct"] for q in after["questions"]] == [True] * 3


def test_feedback_is_per_student(feedback_activity):
    answered = join(name="Answered", ip="10.0.0.1")
    other = join(name="Other", ip="10.0.0.2")
    answer(answered, feedback_activity, 0, RIGHT[0])

    body = other.get(STATE)

    assert [q["response"] for q in body.json()["questions"]] == [None] * 3
    assert secrets_in(body.content.decode()) == set()


def test_feedback_off_never_reveals_answers_even_when_locked(activity):
    student = join()
    answer(student, activity, 0, RIGHT[0])
    Response.objects.update(is_locked=True)

    body = student.get(STATE)

    assert body.json()["questions"][0]["response"]["is_locked"] is True
    assert body.json()["questions"][0]["response"]["feedback"] is None
    assert secrets_in(body.content.decode()) == set()


def test_teacher_paced_with_feedback_locks_on_submit(teacher_client, room, quiz):
    response = start_quiz(teacher_client, room, quiz, show_feedback=True)
    activity = Activity.objects.get(pk=response.json()["activity"]["id"])
    student = join()

    submit = answer(student, activity, 0, WRONG[0])
    retry = answer(student, activity, 0, RIGHT[0])

    assert submit.json()["is_locked"] is True
    assert submit.json()["feedback"]["correct_index"] == 1
    assert retry.json()["code"] == "response_locked"
    assert secrets_in(student.get(STATE).content.decode()) == question_secrets(0)


# --- Teacher progress -------------------------------------------------------


def test_teacher_state_shows_each_students_progress(teacher_client, activity):
    done = join(name="Done", ip="10.0.0.1")
    halfway = join(name="Halfway", ip="10.0.0.2")
    join(name="Idle", ip="10.0.0.3")
    answer(done, activity, 0, RIGHT[0])
    answer(done, activity, 2, WRONG[2])
    done.post(FINISH)
    answer(halfway, activity, 1, WRONG[1])
    answer(halfway, activity, 1, RIGHT[1])  # changing an answer doesn't count twice

    body = teacher_client.get(teacher_state_url(activity)).json()

    progress = {
        p["name"]: (p["answered_count"], p["question_count"], p["finished_at"] is not None)
        for p in body["participants"]
    }
    assert progress == {
        "Done": (2, 3, True),
        "Halfway": (1, 3, False),
        "Idle": (0, 3, False),
    }
    finished_at = Participant.objects.get(name="Done").finished_at
    done_row = next(p for p in body["participants"] if p["name"] == "Done")
    assert done_row["finished_at"] == finished_at.isoformat().replace("+00:00", "Z")
    assert all(r["is_locked"] for r in body["responses"] if r["participant_id"] == done.participant_id)
    assert not any(
        r["is_locked"] for r in body["responses"] if r["participant_id"] == halfway.participant_id
    )


def test_teacher_state_progress_query_count(
    teacher_client, activity, django_assert_max_num_queries
):
    for i in range(5):
        student = join(name=f"S{i}", ip=f"10.0.0.{i}")
        for index in range(i % 4):
            answer(student, activity, index, RIGHT[index])

    with django_assert_max_num_queries(6):
        body = teacher_client.get(teacher_state_url(activity)).json()

    assert sorted(p["answered_count"] for p in body["participants"]) == [0, 0, 1, 2, 3]


def test_navigate_is_not_for_student_paced(teacher_client, activity):
    response = teacher_client.post(navigate_url(activity), {"index": 1}, format="json")

    assert response.status_code == 409
    assert response.json()["code"] == "not_teacher_paced"
