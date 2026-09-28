import pytest
from django.db import IntegrityError, transaction
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from activities import services as activity_services
from activities.models import Activity, ActivityMode, ActivityQuestion, ActivityType
from quizzes import services
from quizzes.models import Quiz, QuizQuestion
from rooms.models import Room

pytestmark = pytest.mark.django_db

QUIZZES = "/api/quizzes"

MC = {
    "type": "MC",
    "prompt": "2 + 2?",
    "choices": ["3", "4", "5"],
    "correct_index": 1,
    "explanation": "Basic addition.",
}
TF = {"type": "TF", "prompt": "The sky is green.", "correct_index": 1}
SA = {"type": "SA", "prompt": "Capital of France?", "accepted_answers": ["Paris"]}


def quiz_url(quiz) -> str:
    return f"{QUIZZES}/{quiz.id}"


def question_rows(quiz) -> list[dict]:
    return list(
        QuizQuestion.objects.filter(quiz=quiz)
        .order_by("order")
        .values("order", "type", "prompt", "explanation", "choices", "correct_index", "accepted_answers")
    )


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
def quiz(teacher):
    return services.create_quiz(owner=teacher, title="Week 1", questions=_full([MC, TF, SA]))


def _full(questions: list[dict]) -> list[dict]:
    """Questions as validated data: every field present, as the serializer hands them over."""
    defaults = {"explanation": "", "choices": [], "correct_index": None, "accepted_answers": []}
    return [{**defaults, **question} for question in questions]


# --- Create -----------------------------------------------------------------


def test_create_quiz_with_every_question_type(teacher_client, teacher):
    response = teacher_client.post(
        QUIZZES, {"title": "  Week 1  ", "questions": [MC, TF, SA]}, format="json"
    )

    assert response.status_code == 201, response.json()
    body = response.json()
    quiz = Quiz.objects.get()
    assert quiz.owner == teacher
    assert body == {
        "id": quiz.id,
        "title": "Week 1",
        "question_count": 3,
        "created_at": body["created_at"],
        "updated_at": body["updated_at"],
        "questions": [
            {
                "order": 0,
                "type": "MC",
                "prompt": "2 + 2?",
                "explanation": "Basic addition.",
                "choices": ["3", "4", "5"],
                "correct_index": 1,
                "accepted_answers": [],
            },
            {
                "order": 1,
                "type": "TF",
                "prompt": "The sky is green.",
                "explanation": "",
                "choices": [],
                "correct_index": 1,
                "accepted_answers": [],
            },
            {
                "order": 2,
                "type": "SA",
                "prompt": "Capital of France?",
                "explanation": "",
                "choices": [],
                "correct_index": None,
                "accepted_answers": ["Paris"],
            },
        ],
    }


def test_text_is_trimmed(teacher_client):
    question = {
        "type": "SA",
        "prompt": "  Name a prime  ",
        "explanation": " Two is prime. ",
        "accepted_answers": [" 2 ", "two"],
    }
    response = teacher_client.post(QUIZZES, {"title": "T", "questions": [question]}, format="json")

    assert response.status_code == 201
    saved = response.json()["questions"][0]
    assert saved["prompt"] == "Name a prime"
    assert saved["explanation"] == "Two is prime."
    assert saved["accepted_answers"] == ["2", "two"]


def test_questions_without_a_correct_answer_are_allowed(teacher_client):
    questions = [
        {"type": "MC", "prompt": "Favourite?", "choices": ["A", "B"], "correct_index": None},
        {"type": "TF", "prompt": "Opinion?"},
        {"type": "SA", "prompt": "Thoughts?", "accepted_answers": []},
    ]
    response = teacher_client.post(QUIZZES, {"title": "Poll", "questions": questions}, format="json")

    assert response.status_code == 201
    assert [q["correct_index"] for q in response.json()["questions"]] == [None, None, None]


def test_limits_are_inclusive(teacher_client):
    """The largest allowed quiz: 200-char title, 100 questions, 6 choices of 300 chars."""
    mc = {
        "type": "MC",
        "prompt": "p" * 1000,
        "explanation": "e" * 2000,
        "choices": ["c" * 300] * 6,
        "correct_index": 5,
    }
    sa = {"type": "SA", "prompt": "p", "accepted_answers": ["a" * 500] * 20}
    response = teacher_client.post(
        QUIZZES, {"title": "t" * 200, "questions": [mc] * 99 + [sa]}, format="json"
    )

    assert response.status_code == 201, response.json()
    assert response.json()["question_count"] == 100
    assert QuizQuestion.objects.count() == 100


def test_tf_correct_index_zero_means_true(teacher_client):
    response = teacher_client.post(
        QUIZZES,
        {"title": "T", "questions": [{"type": "TF", "prompt": "1 < 2", "correct_index": 0}]},
        format="json",
    )

    assert response.status_code == 201
    assert response.json()["questions"][0]["correct_index"] == 0


# --- Validation (PRD Q1–Q5) -------------------------------------------------


def with_question(**fields) -> dict:
    return {"title": "T", "questions": [fields]}


@pytest.mark.parametrize(
    "data, field",
    [
        # Q1: title and 1–100 questions.
        ({"questions": [MC]}, "title"),
        ({"title": "   ", "questions": [MC]}, "title"),
        ({"title": "t" * 201, "questions": [MC]}, "title"),
        ({"title": "T"}, "questions"),
        ({"title": "T", "questions": []}, "questions"),
        ({"title": "T", "questions": [MC] * 101}, "questions"),
        ({"title": "T", "questions": "not a list"}, "questions"),
        ({"title": "T", "questions": ["not an object"]}, "questions.0"),
        # Every type: a type and a prompt of 1–1000 characters.
        (with_question(prompt="Q?"), "questions.0.type"),
        (with_question(type="XX", prompt="Q?"), "questions.0.type"),
        (with_question(type="SA"), "questions.0.prompt"),
        (with_question(type="SA", prompt="   "), "questions.0.prompt"),
        (with_question(type="SA", prompt="p" * 1001), "questions.0.prompt"),
        (with_question(type="TF", prompt="p" * 1001), "questions.0.prompt"),
        (with_question(type="SA", prompt="Q?", explanation="e" * 2001), "questions.0.explanation"),
        # Q3: MC has 2–6 choices of 1–300 characters, correct_index among them.
        (with_question(type="MC", prompt="Q?"), "questions.0.choices"),
        (with_question(type="MC", prompt="Q?", choices=["only"]), "questions.0.choices"),
        (with_question(type="MC", prompt="Q?", choices=list("ABCDEFG")), "questions.0.choices"),
        (with_question(type="MC", prompt="Q?", choices=["A", "  "]), "questions.0.choices.1"),
        (with_question(type="MC", prompt="Q?", choices=["A", "c" * 301]), "questions.0.choices.1"),
        (with_question(type="MC", prompt="Q?", choices=["A", "B"], correct_index=2), "questions.0.correct_index"),
        (with_question(type="MC", prompt="Q?", choices=["A", "B"], correct_index=-1), "questions.0.correct_index"),
        (with_question(type="MC", prompt="Q?", choices=["A", "B"], correct_index="x"), "questions.0.correct_index"),
        (with_question(type="MC", prompt="Q?", choices=["A", "B"], accepted_answers=["A"]), "questions.0.accepted_answers"),
        # Q4: TF has implicit True/False choices; correct_index is 0, 1 or null.
        (with_question(type="TF", prompt="Q?", choices=["True", "False"]), "questions.0.choices"),
        (with_question(type="TF", prompt="Q?", correct_index=2), "questions.0.correct_index"),
        (with_question(type="TF", prompt="Q?", accepted_answers=["True"]), "questions.0.accepted_answers"),
        # Q5: SA has accepted answers (up to 20 of 1–500 characters), never correct_index.
        (with_question(type="SA", prompt="Q?", correct_index=0), "questions.0.correct_index"),
        (with_question(type="SA", prompt="Q?", choices=["A", "B"]), "questions.0.choices"),
        (with_question(type="SA", prompt="Q?", accepted_answers=["ok", " "]), "questions.0.accepted_answers.1"),
        (with_question(type="SA", prompt="Q?", accepted_answers=["a" * 501]), "questions.0.accepted_answers.0"),
        (with_question(type="SA", prompt="Q?", accepted_answers=["a"] * 21), "questions.0.accepted_answers"),
    ],
)
def test_create_rejects_invalid_quiz(teacher_client, data, field):
    response = teacher_client.post(QUIZZES, data, format="json")

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "validation_error"
    assert field in body["fields"], body
    assert not Quiz.objects.exists()
    assert not QuizQuestion.objects.exists()


def test_errors_point_at_the_question_at_fault(teacher_client):
    bad_mc = {"type": "MC", "prompt": "Q?", "choices": ["A"]}
    bad_sa = {"type": "SA", "prompt": "", "correct_index": 0}
    response = teacher_client.post(
        QUIZZES, {"title": "T", "questions": [MC, bad_mc, TF, bad_sa]}, format="json"
    )

    assert response.status_code == 400
    assert response.json()["fields"] == {
        "questions.1.choices": ["Multiple choice questions need 2–6 choices."],
        "questions.3.prompt": ["This field may not be blank."],
    }


def test_type_rules_are_reported_together(teacher_client):
    question = {"type": "SA", "prompt": "Q?", "choices": ["A", "B"], "correct_index": 0}
    response = teacher_client.post(QUIZZES, with_question(**question), format="json")

    assert response.status_code == 400
    assert set(response.json()["fields"]) == {"questions.0.choices", "questions.0.correct_index"}


# --- List and retrieve ------------------------------------------------------


def test_list_own_quizzes_most_recently_edited_first(teacher_client, teacher, other_teacher, quiz):
    newer = services.create_quiz(owner=teacher, title="Week 2", questions=_full([TF]))
    services.create_quiz(owner=other_teacher, title="Bob's", questions=_full([TF]))

    response = teacher_client.get(QUIZZES)

    assert response.status_code == 200
    assert [
        {key: item[key] for key in ("id", "title", "question_count")} for item in response.json()
    ] == [
        {"id": newer.id, "title": "Week 2", "question_count": 1},
        {"id": quiz.id, "title": "Week 1", "question_count": 3},
    ]
    assert set(response.json()[0]) == {"id", "title", "question_count", "created_at", "updated_at"}


def test_retrieve_returns_questions_in_order(teacher_client, quiz):
    response = teacher_client.get(quiz_url(quiz))

    assert response.status_code == 200
    body = response.json()
    assert body["question_count"] == 3
    assert [(q["order"], q["type"]) for q in body["questions"]] == [(0, "MC"), (1, "TF"), (2, "SA")]


def test_retrieved_quiz_can_be_saved_back_unchanged(teacher_client, quiz):
    """The editor PUTs back what it GOT (TF comes back with choices: [])."""
    body = teacher_client.get(quiz_url(quiz)).json()

    response = teacher_client.put(quiz_url(quiz), body, format="json")

    assert response.status_code == 200, response.json()
    assert response.json()["questions"] == body["questions"]


# --- Replace (PUT) ----------------------------------------------------------


def test_put_replaces_title_and_all_questions(teacher_client, quiz):
    before = Quiz.objects.get(pk=quiz.pk).updated_at
    new_questions = [SA, {**MC, "choices": ["x", "y"], "correct_index": 0}]

    response = teacher_client.put(
        quiz_url(quiz), {"title": "Week 1 (revised)", "questions": new_questions}, format="json"
    )

    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["title"] == "Week 1 (revised)"
    assert body["question_count"] == 2
    assert [(q["order"], q["type"]) for q in body["questions"]] == [(0, "SA"), (1, "MC")]
    quiz.refresh_from_db()
    assert quiz.title == "Week 1 (revised)"
    assert quiz.updated_at > before
    assert [row["type"] for row in question_rows(quiz)] == ["SA", "MC"]
    assert question_rows(quiz)[1]["choices"] == ["x", "y"]


def test_put_reorders_questions(teacher_client, quiz):
    response = teacher_client.put(quiz_url(quiz), {"title": "Week 1", "questions": [SA, TF, MC]}, format="json")

    assert response.status_code == 200
    assert [(row["order"], row["type"]) for row in question_rows(quiz)] == [(0, "SA"), (1, "TF"), (2, "MC")]


@pytest.mark.parametrize(
    "data",
    [
        {"title": "New title", "questions": [MC, {"type": "MC", "prompt": "Q?", "choices": ["A"]}]},
        {"title": "New title", "questions": []},
        {"title": "", "questions": [SA]},
        {"questions": [SA]},
        {"title": "New title"},
    ],
)
def test_invalid_put_changes_nothing(teacher_client, quiz, data):
    before = question_rows(quiz)

    response = teacher_client.put(quiz_url(quiz), data, format="json")

    assert response.status_code == 400
    quiz.refresh_from_db()
    assert quiz.title == "Week 1"
    assert question_rows(quiz) == before


def test_patch_is_not_supported(teacher_client, quiz):
    response = teacher_client.patch(quiz_url(quiz), {"title": "X"}, format="json")

    assert response.status_code == 405
    quiz.refresh_from_db()
    assert quiz.title == "Week 1"


# --- Duplicate --------------------------------------------------------------


def test_duplicate_copies_title_and_questions(teacher_client, teacher, quiz):
    response = teacher_client.post(f"{quiz_url(quiz)}/duplicate")

    assert response.status_code == 201, response.json()
    body = response.json()
    copy = Quiz.objects.get(pk=body["id"])
    assert copy.pk != quiz.pk
    assert copy.owner == teacher
    assert body["title"] == copy.title == "Week 1 (copy)"
    assert body["question_count"] == 3
    assert question_rows(copy) == question_rows(quiz)
    assert body["questions"] == teacher_client.get(quiz_url(quiz)).json()["questions"]


def test_duplicate_is_independent_of_the_original(teacher_client, quiz):
    copy_id = teacher_client.post(f"{quiz_url(quiz)}/duplicate").json()["id"]
    original = question_rows(quiz)

    teacher_client.put(f"{QUIZZES}/{copy_id}", {"title": "Edited", "questions": [TF]}, format="json")
    teacher_client.delete(f"{QUIZZES}/{copy_id}")

    quiz.refresh_from_db()
    assert quiz.title == "Week 1"
    assert question_rows(quiz) == original


def test_duplicate_of_a_max_length_title_stays_within_the_limit(teacher, teacher_client):
    long = services.create_quiz(owner=teacher, title="t" * 200, questions=_full([TF]))

    response = teacher_client.post(f"{quiz_url(long)}/duplicate")

    assert response.status_code == 201
    title = response.json()["title"]
    assert len(title) == 200
    assert title == "t" * 193 + " (copy)"


def test_duplicating_a_copy_appends_again(teacher_client, quiz):
    copy_id = teacher_client.post(f"{quiz_url(quiz)}/duplicate").json()["id"]

    response = teacher_client.post(f"{QUIZZES}/{copy_id}/duplicate")

    assert response.json()["title"] == "Week 1 (copy) (copy)"


# --- Delete -----------------------------------------------------------------


def test_delete_quiz_removes_its_questions(teacher_client, quiz):
    response = teacher_client.delete(quiz_url(quiz))

    assert response.status_code == 204
    assert not Quiz.objects.exists()
    assert not QuizQuestion.objects.exists()
    assert teacher_client.get(quiz_url(quiz)).status_code == 404


def test_delete_quiz_keeps_past_activity_reports(teacher_client, teacher, quiz):
    """Q6: activities snapshot questions, so deleting the quiz leaves reports intact."""
    room = Room.objects.create(owner=teacher, name="Bio", code="BIO3AB")
    activity = activity_services._launch(
        room,
        type=ActivityType.QUIZ,
        mode=ActivityMode.TEACHER_PACED,
        questions=[
            {name: row[name] for name in services.QUESTION_FIELDS} for row in question_rows(quiz)
        ],
        quiz_title=quiz.title,
        source_quiz=quiz,
    )

    assert teacher_client.delete(quiz_url(quiz)).status_code == 204

    activity.refresh_from_db()
    assert activity.source_quiz is None
    assert activity.quiz_title == "Week 1"
    assert list(ActivityQuestion.objects.filter(activity=activity).values_list("prompt", flat=True)) == [
        "2 + 2?",
        "The sky is green.",
        "Capital of France?",
    ]
    assert Activity.objects.count() == 1


# --- Ownership and auth -----------------------------------------------------


@pytest.mark.parametrize(
    "method, suffix, data",
    [
        ("get", "", None),
        ("put", "", {"title": "Mine now", "questions": [TF]}),
        ("delete", "", None),
        ("post", "/duplicate", None),
    ],
)
def test_other_teachers_quiz_is_404(other_client, quiz, method, suffix, data):
    before = question_rows(quiz)

    response = getattr(other_client, method)(f"{quiz_url(quiz)}{suffix}", data, format="json")

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    quiz.refresh_from_db()
    assert quiz.title == "Week 1"
    assert question_rows(quiz) == before
    assert Quiz.objects.count() == 1


def test_other_teacher_does_not_see_quiz_in_list(other_client, quiz):
    assert other_client.get(QUIZZES).json() == []


def test_unknown_quiz_is_404(teacher_client):
    assert teacher_client.get(f"{QUIZZES}/999999").status_code == 404


@pytest.mark.parametrize(
    "method, path",
    [
        ("get", QUIZZES),
        ("post", QUIZZES),
        ("get", f"{QUIZZES}/1"),
        ("put", f"{QUIZZES}/1"),
        ("delete", f"{QUIZZES}/1"),
        ("post", f"{QUIZZES}/1/duplicate"),
    ],
)
def test_quiz_endpoints_require_auth(api_client, method, path):
    response = getattr(api_client, method)(path, {}, format="json")

    assert response.status_code == 401


# --- Model constraints ------------------------------------------------------


def test_question_order_is_unique_per_quiz(quiz):
    with pytest.raises(IntegrityError), transaction.atomic():
        QuizQuestion.objects.create(quiz=quiz, order=0, type="TF", prompt="Dup")
