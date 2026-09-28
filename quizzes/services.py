"""Quizzes: create, replace, duplicate, delete (PRD §5.3, §8).

Questions arrive already validated by `quizzes.serializers.QuizWriteSerializer`. A quiz's
questions are always written as a whole: the list order becomes `order`, and saving
replaces every existing question.
"""

from django.contrib.auth.models import AbstractBaseUser
from django.db import transaction
from django.db.models import Count, QuerySet

from .models import Quiz, QuizQuestion

COPY_SUFFIX = " (copy)"
TITLE_MAX_LENGTH = Quiz._meta.get_field("title").max_length

QUESTION_FIELDS = ["type", "prompt", "explanation", "choices", "correct_index", "accepted_answers"]


def _with_question_count(quizzes: QuerySet[Quiz]) -> QuerySet[Quiz]:
    # Django ignores Meta.ordering on aggregated queries, so order explicitly.
    return quizzes.annotate(question_count=Count("questions")).order_by(*Quiz._meta.ordering)


def owned_quizzes(owner: AbstractBaseUser) -> QuerySet[Quiz]:
    """The teacher's quizzes, most recently edited first, each with `question_count`.
    Other teachers' quizzes are simply absent, so lookups 404."""
    return _with_question_count(Quiz.objects.filter(owner=owner))


def _reload(quiz: Quiz) -> Quiz:
    """The quiz with `question_count` and its questions, fresh from the DB."""
    return _with_question_count(Quiz.objects.prefetch_related("questions")).get(pk=quiz.pk)


def _write_questions(quiz: Quiz, questions: list[dict]) -> None:
    QuizQuestion.objects.bulk_create(
        QuizQuestion(quiz=quiz, order=order, **{name: question[name] for name in QUESTION_FIELDS})
        for order, question in enumerate(questions)
    )


def create_quiz(*, owner: AbstractBaseUser, title: str, questions: list[dict]) -> Quiz:
    with transaction.atomic():
        quiz = Quiz.objects.create(owner=owner, title=title)
        _write_questions(quiz, questions)
    return _reload(quiz)


def replace_quiz(quiz: Quiz, *, title: str, questions: list[dict]) -> Quiz:
    """Replace the title and the full questions array in one transaction (PRD §8 PUT).

    The quiz row is locked first, so two concurrent saves apply one after the other
    (the last one wins) instead of clashing on question order.
    """
    with transaction.atomic():
        quiz = Quiz.objects.select_for_update().get(pk=quiz.pk)
        quiz.title = title
        quiz.save(update_fields=["title", "updated_at"])
        quiz.questions.all().delete()
        _write_questions(quiz, questions)
    return _reload(quiz)


def copy_title(title: str) -> str:
    """`"<title> (copy)"`, shortening the title if needed to stay within 200 characters."""
    return title[: TITLE_MAX_LENGTH - len(COPY_SUFFIX)].rstrip() + COPY_SUFFIX


def duplicate_quiz(quiz: Quiz) -> Quiz:
    """A new quiz for the same teacher with the same questions (PRD Q6)."""
    questions = [
        {name: getattr(question, name) for name in QUESTION_FIELDS}
        for question in quiz.questions.order_by("order")
    ]
    return create_quiz(owner=quiz.owner, title=copy_title(quiz.title), questions=questions)


def delete_quiz(quiz: Quiz) -> None:
    """Deletes the quiz and its questions. Activities launched from it keep their
    question snapshots; their `source_quiz` becomes null (PRD Q6)."""
    quiz.delete()
