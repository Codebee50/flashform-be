"""Shuffle question order: in a student-paced quiz, each student gets the questions in their
own random order, fixed for the activity; the teacher's view and the report keep the quiz
order (PRD §5.5 A3, §7)."""

from unittest import mock

import pytest

from activities import services
from activities.models import ActivityMode, ActivityType, Participant
from tests.test_activities import (  # noqa: F401 (fixtures)
    ANSWER_KEYS,
    STATE,
    join,
    keys_in,
    question,
    response_url,
    room,
    teacher_state_url,
)
from tests.test_quiz_activities import PROMPTS, quiz, start_quiz  # noqa: F401 (fixtures)
from tests.test_student_paced import FINISH, RIGHT, start_student_paced

pytestmark = pytest.mark.django_db


def reverse_sample(population, k):
    return list(reversed(population))[:k]


def shuffled_launch(room, count: int):
    questions = [{"type": "TF", "prompt": f"Q{i}"} for i in range(count)]
    return services._launch(
        room,
        type=ActivityType.QUIZ,
        mode=ActivityMode.STUDENT_PACED,
        questions=questions,
        shuffle_questions=True,
    )


def test_start_a_shuffled_student_paced_quiz(teacher_client, room, quiz):
    response = start_quiz(
        teacher_client, room, quiz, mode="STUDENT_PACED", shuffle_questions=True
    )

    assert response.status_code == 201, response.json()
    assert response.json()["activity"]["shuffle_questions"] is True
    # The teacher sees the quiz order.
    assert [q["order"] for q in response.json()["questions"]] == [0, 1, 2]


def test_shuffling_is_only_for_student_paced(teacher_client, room, quiz):
    response = start_quiz(teacher_client, room, quiz, shuffle_questions=True)

    assert response.status_code == 400
    assert response.json()["fields"]["shuffle_questions"] == [
        "Shuffling is only for student-paced quizzes."
    ]


def test_each_student_sees_their_own_order(teacher_client, room, quiz):
    start_student_paced(teacher_client, room, quiz, shuffle_questions=True)
    with mock.patch("activities.services.random.sample", side_effect=reverse_sample):
        student = join()

    body = student.get(STATE).json()

    assert Participant.objects.get().question_order == [2, 1, 0]
    assert [q["order"] for q in body["questions"]] == [2, 1, 0]
    assert [q["prompt"] for q in body["questions"]] == PROMPTS[::-1]
    assert body["question_count"] == 3
    assert keys_in(body) & ANSWER_KEYS == set()
    # Fixed for the activity: every refetch gives the same order.
    assert student.get(STATE).json() == body


def test_orders_are_random_permutations_per_student(room):
    shuffled_launch(room, 8)

    for i in range(5):
        join(name=f"S{i}", ip=f"10.0.0.{i + 1}")

    orders = [p.question_order for p in Participant.objects.all()]
    assert all(sorted(order) == list(range(8)) for order in orders)
    # Five identical draws out of 8! orders would be astronomically unlikely.
    assert len({tuple(order) for order in orders}) > 1


def test_without_shuffle_there_is_no_order(teacher_client, room, quiz):
    start_student_paced(teacher_client, room, quiz)
    student = join()

    body = student.get(STATE).json()

    assert Participant.objects.get().question_order is None
    assert [q["order"] for q in body["questions"]] == [0, 1, 2]


def test_answers_and_finish_work_in_shuffled_order(teacher_client, room, quiz):
    activity = start_student_paced(teacher_client, room, quiz, shuffle_questions=True)
    with mock.patch("activities.services.random.sample", side_effect=reverse_sample):
        student = join()

    for index in (2, 0):
        submit = student.put(response_url(question(activity, index)), RIGHT[index], format="json")
        assert submit.status_code == 200, submit.json()
    finished = student.post(FINISH).json()

    assert [q["order"] for q in finished["questions"]] == [2, 1, 0]
    assert [bool(q["response"]) for q in finished["questions"]] == [True, False, True]
    teacher = teacher_client.get(teacher_state_url(activity)).json()
    assert [q["order"] for q in teacher["questions"]] == [0, 1, 2]
    assert teacher["participants"][0]["answered_count"] == 2
    assert teacher["participants"][0]["score"] == 2
