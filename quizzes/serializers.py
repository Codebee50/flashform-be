"""Request and response shapes for quizzes (PRD §8), and the question rules (PRD Q1–Q5).

`QuizWriteSerializer` is the only way quiz content gets in, and it enforces every rule:
1–100 questions; a prompt (≤ 1000 chars) on every question; MC takes 2–6 choices
(each ≤ 300 chars) and an optional `correct_index` among them; TF takes no choices
(["True", "False"] is implied) and an optional `correct_index` of 0 (True) or 1 (False);
SA takes optional `accepted_answers` and no `correct_index`. Fields that don't apply to a
question's type must be left out or empty.

Errors inside questions are reported under dotted paths such as `questions.2.choices`
(0-based), so the editor can point at the question at fault.
"""

from rest_framework import serializers
from rest_framework.settings import api_settings

from activities.models import TF_CHOICES, QuestionType

from .models import Quiz, QuizQuestion

MIN_QUESTIONS, MAX_QUESTIONS = 1, 100
MIN_CHOICES, MAX_CHOICES = 2, 6
MAX_ACCEPTED_ANSWERS = 20

NON_FIELD_KEYS = (api_settings.NON_FIELD_ERRORS_KEY, "detail")


def flatten_errors(detail) -> dict:
    """Turn nested serializer errors into `{"questions.2.choices": [...]}`.

    A nested non-field error (e.g. a list length error on `questions`) is reported under
    its parent's path (`questions`).
    """
    flat: dict[str, list] = {}

    def walk(value, path: str) -> None:
        if isinstance(value, dict):
            items = value.items()
        elif isinstance(value, list) and any(isinstance(item, (dict, list)) for item in value):
            items = enumerate(value)  # one entry per list item; valid items are empty
        else:
            messages = value if isinstance(value, list) else [value]
            flat.setdefault(path or api_settings.NON_FIELD_ERRORS_KEY, []).extend(messages)
            return
        for key, child in items:
            if key in NON_FIELD_KEYS and path:
                walk(child, path)
            else:
                walk(child, f"{path}.{key}" if path else str(key))

    walk(detail, "")
    return flat


# --- Requests ---------------------------------------------------------------


class QuizQuestionWriteSerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=QuestionType.choices)
    prompt = serializers.CharField(max_length=1000, help_text="Required, 1–1000 characters, trimmed.")
    explanation = serializers.CharField(
        max_length=2000,
        required=False,
        allow_blank=True,
        default="",
        help_text="Optional, ≤ 2000 characters. Shown to students with feedback.",
    )
    choices = serializers.ListField(
        child=serializers.CharField(max_length=300),
        required=False,
        default=list,
        help_text="MC only: 2–6 non-blank options, each ≤ 300 characters. TF and SA: omit or "
        "send []; TF always uses " + ", ".join(TF_CHOICES) + ".",
    )
    correct_index = serializers.IntegerField(
        min_value=0,
        required=False,
        allow_null=True,
        default=None,
        help_text="MC: index into `choices`. TF: 0 = True, 1 = False. Null (or omitted) for "
        "no correct answer. SA: must be null.",
    )
    accepted_answers = serializers.ListField(
        child=serializers.CharField(max_length=500),
        required=False,
        default=list,
        max_length=MAX_ACCEPTED_ANSWERS,
        help_text=f"SA only: up to {MAX_ACCEPTED_ANSWERS} non-blank answers, each ≤ 500 "
        "characters. Students match case-insensitively after trimming. Empty means no "
        "correct answer. MC and TF: omit or send [].",
    )

    def validate(self, attrs):
        type_ = attrs["type"]
        choices = attrs["choices"]
        correct_index = attrs["correct_index"]
        errors = {}

        if type_ == QuestionType.MC:
            if not MIN_CHOICES <= len(choices) <= MAX_CHOICES:
                errors["choices"] = [
                    f"Multiple choice questions need {MIN_CHOICES}–{MAX_CHOICES} choices."
                ]
            elif correct_index is not None and correct_index >= len(choices):
                errors["correct_index"] = [
                    f"Must be the index of one of the choices (0–{len(choices) - 1}) or null."
                ]
        elif choices:
            errors["choices"] = [
                "Only multiple choice questions take choices; true/false questions always "
                "use True and False."
            ]

        if type_ == QuestionType.TF and correct_index not in (None, 0, 1):
            errors["correct_index"] = ["Must be 0 (True), 1 (False) or null."]

        if type_ == QuestionType.SA:
            if correct_index is not None:
                errors["correct_index"] = [
                    "Short answer questions use accepted_answers instead of correct_index."
                ]
        elif attrs["accepted_answers"]:
            errors["accepted_answers"] = ["Only short answer questions take accepted answers."]

        if errors:
            raise serializers.ValidationError(errors)
        return attrs


class QuizWriteSerializer(serializers.Serializer):
    """Body of POST /quizzes and PUT /quizzes/{id}: the whole quiz."""

    title = serializers.CharField(max_length=200, help_text="Required, 1–200 characters, trimmed.")
    questions = QuizQuestionWriteSerializer(
        many=True,
        allow_empty=False,
        min_length=MIN_QUESTIONS,
        max_length=MAX_QUESTIONS,
        help_text=f"{MIN_QUESTIONS}–{MAX_QUESTIONS} questions, in order.",
    )

    def to_internal_value(self, data):
        try:
            return super().to_internal_value(data)
        except serializers.ValidationError as exc:
            raise serializers.ValidationError(flatten_errors(exc.detail))


# --- Responses --------------------------------------------------------------


class QuizQuestionSerializer(serializers.ModelSerializer):
    choices = serializers.ListField(
        child=serializers.CharField(), help_text="MC options; always [] for TF and SA."
    )
    accepted_answers = serializers.ListField(
        child=serializers.CharField(), help_text="SA only; [] otherwise."
    )

    class Meta:
        model = QuizQuestion
        fields = ["order", "type", "prompt", "explanation", "choices", "correct_index", "accepted_answers"]
        read_only_fields = fields


class QuizListItemSerializer(serializers.ModelSerializer):
    question_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Quiz
        fields = ["id", "title", "question_count", "created_at", "updated_at"]
        read_only_fields = fields


class QuizSerializer(QuizListItemSerializer):
    questions = QuizQuestionSerializer(many=True, read_only=True)

    class Meta(QuizListItemSerializer.Meta):
        fields = [*QuizListItemSerializer.Meta.fields, "questions"]
        read_only_fields = fields
