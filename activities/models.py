"""Activities, their question snapshots, participants and responses (PRD §4, §7)."""

import uuid

from django.db import models
from django.utils import timezone


class ActivityType(models.TextChoices):
    QUICK = "QUICK"
    QUIZ = "QUIZ"


class ActivityMode(models.TextChoices):
    TEACHER_PACED = "TEACHER_PACED"
    STUDENT_PACED = "STUDENT_PACED"


class ActivityStatus(models.TextChoices):
    LIVE = "LIVE"
    ENDED = "ENDED"


class QuestionType(models.TextChoices):
    MC = "MC"
    TF = "TF"
    SA = "SA"


# TF questions store no choices (PRD §7); these are implied.
TF_CHOICES = ["True", "False"]


class Activity(models.Model):
    """One run of a quick question or quiz in a room.

    `version` is incremented in the same transaction as every state change (PRD §9), so
    clients can tell when they missed an event and must refetch.
    """

    room = models.ForeignKey("rooms.Room", on_delete=models.CASCADE, related_name="activities")
    type = models.CharField(max_length=5, choices=ActivityType.choices)
    mode = models.CharField(
        max_length=13, choices=ActivityMode.choices, default=ActivityMode.TEACHER_PACED
    )
    quiz_title = models.CharField(max_length=200, blank=True)
    source_quiz = models.ForeignKey(
        "quizzes.Quiz",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="activities",
    )
    status = models.CharField(
        max_length=5, choices=ActivityStatus.choices, default=ActivityStatus.LIVE
    )
    current_index = models.PositiveIntegerField(default=0)
    show_feedback = models.BooleanField(default=False)
    shuffle_questions = models.BooleanField(default=False)
    hide_results = models.BooleanField(default=False)
    version = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField(default=timezone.now)
    ended_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-started_at", "-id"]
        verbose_name_plural = "activities"
        constraints = [
            models.UniqueConstraint(
                fields=["room"],
                condition=models.Q(status="LIVE"),
                name="one_live_activity_per_room",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.type} #{self.pk} in {self.room_id} ({self.status})"

    @property
    def is_live(self) -> bool:
        return self.status == ActivityStatus.LIVE


class ActivityQuestion(models.Model):
    """A question as it was when the activity launched, so later quiz edits don't
    change past reports (PRD §7)."""

    activity = models.ForeignKey(Activity, on_delete=models.CASCADE, related_name="questions")
    order = models.PositiveIntegerField()
    type = models.CharField(max_length=2, choices=QuestionType.choices)
    prompt = models.TextField(blank=True)
    explanation = models.TextField(blank=True)
    choices = models.JSONField(default=list, blank=True)  # MC only
    correct_index = models.PositiveIntegerField(null=True, blank=True)  # MC/TF
    accepted_answers = models.JSONField(default=list, blank=True)  # SA only
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["activity", "order"]
        constraints = [
            models.UniqueConstraint(
                fields=["activity", "order"], name="activity_question_order_unique"
            ),
        ]

    def __str__(self) -> str:
        return f"Q{self.order + 1} ({self.type}) of activity {self.activity_id}"

    @property
    def display_choices(self) -> list[str]:
        """The options a student picks from: MC choices, True/False, or none (SA)."""
        if self.type == QuestionType.MC:
            return list(self.choices)
        if self.type == QuestionType.TF:
            return list(TF_CHOICES)
        return []

    @property
    def has_correct_answer(self) -> bool:
        if self.type == QuestionType.SA:
            return bool(self.accepted_answers)
        return self.correct_index is not None


class Participant(models.Model):
    """A student inside one activity. Authenticates with a secret token whose sha256 is
    `token_hash`; the raw token is only ever sent to the student once, at join."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    activity = models.ForeignKey(Activity, on_delete=models.CASCADE, related_name="participants")
    name = models.CharField(max_length=40)
    token_hash = models.CharField(max_length=64, unique=True)
    question_order = models.JSONField(null=True, blank=True)
    is_removed = models.BooleanField(default=False)
    finished_at = models.DateTimeField(null=True, blank=True)
    last_seen_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self) -> str:
        return f"{self.name} in activity {self.activity_id}"


class Response(models.Model):
    """One participant's answer to one question. Submitting again upserts (PRD §6 rule 5)."""

    participant = models.ForeignKey(
        Participant, on_delete=models.CASCADE, related_name="responses"
    )
    question = models.ForeignKey(
        ActivityQuestion, on_delete=models.CASCADE, related_name="responses"
    )
    choice_index = models.PositiveIntegerField(null=True, blank=True)  # MC/TF
    text_answer = models.CharField(max_length=500, blank=True)  # SA
    is_correct = models.BooleanField(null=True, blank=True)  # null: no correct answer
    is_locked = models.BooleanField(default=False)
    submitted_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["submitted_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["participant", "question"], name="one_response_per_participant_question"
            ),
        ]

    def __str__(self) -> str:
        return f"Response of {self.participant_id} to question {self.question_id}"
