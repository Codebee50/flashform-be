from django.conf import settings
from django.db import models

from activities.models import QuestionType


class Quiz(models.Model):
    """A saved, reusable list of questions owned by a teacher (PRD §4, §7).

    Activities snapshot a quiz's questions at launch (`ActivityQuestion`), so editing or
    deleting a quiz never changes past reports.
    """

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="quizzes"
    )
    title = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "-id"]

    def __str__(self) -> str:
        return self.title


class QuizQuestion(models.Model):
    """One question of a quiz. Same fields as `ActivityQuestion`, which copies them.

    The rules per type (PRD Q3–Q5) are enforced by `quizzes.serializers`: MC has 2–6
    `choices`; TF stores no choices (["True", "False"] is implied, see `TF_CHOICES`); only
    SA has `accepted_answers`, and it never has a `correct_index`.
    """

    quiz = models.ForeignKey(Quiz, on_delete=models.CASCADE, related_name="questions")
    order = models.PositiveIntegerField()  # 0-based
    type = models.CharField(max_length=2, choices=QuestionType.choices)
    prompt = models.TextField()
    explanation = models.TextField(blank=True)
    choices = models.JSONField(default=list, blank=True)  # MC only
    correct_index = models.PositiveIntegerField(null=True, blank=True)  # MC/TF
    accepted_answers = models.JSONField(default=list, blank=True)  # SA only
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["quiz", "order"]
        constraints = [
            models.UniqueConstraint(fields=["quiz", "order"], name="quiz_question_order_unique"),
        ]

    def __str__(self) -> str:
        return f"Q{self.order + 1} ({self.type}) of quiz {self.quiz_id}"
