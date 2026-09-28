"""Request and response shapes for activities (PRD §8).

Student-facing serializers deliberately have no correct-answer fields: correctness only
reaches students through `FeedbackSerializer`, which `services.feedback_for` fills only
when feedback is on and the student's response is locked.
"""

from rest_framework import serializers

from .models import (
    Activity,
    ActivityMode,
    ActivityQuestion,
    ActivityType,
    Participant,
    QuestionType,
    Response,
)

# --- Requests ---------------------------------------------------------------


class QuickQuestionSerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=QuestionType.choices)
    prompt = serializers.CharField(
        max_length=1000,
        required=False,
        allow_blank=True,
        default="",
        help_text="Optional; teachers often ask out loud. Empty means students see "
        '"Answer the question your teacher asked."',
    )
    choices = serializers.ListField(
        child=serializers.CharField(max_length=300),
        min_length=2,
        max_length=6,
        required=False,
        help_text="MC only: 2–6 option labels. Omit for the default A, B, C, D.",
    )

    def validate(self, attrs):
        if "choices" in attrs and attrs["type"] != QuestionType.MC:
            raise serializers.ValidationError(
                {"choices": ["Only multiple choice questions take choices."]}
            )
        return attrs


class ActivityStartSerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=ActivityType.choices)
    question = QuickQuestionSerializer(required=False, help_text="QUICK only, required.")
    quiz_id = serializers.IntegerField(required=False, help_text="QUIZ only, required.")
    mode = serializers.ChoiceField(
        choices=ActivityMode.choices,
        required=False,
        help_text="QUIZ only, required. Only TEACHER_PACED is supported for now.",
    )
    show_feedback = serializers.BooleanField(
        default=False,
        help_text="QUIZ only: after submitting, students see correct/incorrect and the "
        "explanation, and can no longer change that answer.",
    )
    shuffle_questions = serializers.BooleanField(
        default=False, help_text="QUIZ only, student-paced only."
    )

    def validate(self, attrs):
        if attrs["type"] == ActivityType.QUICK:
            if "question" not in attrs:
                raise serializers.ValidationError({"question": ["This field is required."]})
            return attrs

        errors = {
            name: ["This field is required."] for name in ("quiz_id", "mode") if name not in attrs
        }
        if errors:
            raise serializers.ValidationError(errors)
        if attrs["mode"] == ActivityMode.STUDENT_PACED:
            raise serializers.ValidationError(
                {"mode": ["Student-paced quizzes are not supported yet."]}, code="not_supported"
            )
        if attrs["shuffle_questions"]:
            raise serializers.ValidationError(
                {"shuffle_questions": ["Shuffling is only for student-paced quizzes."]}
            )
        return attrs


class NavigateSerializer(serializers.Serializer):
    index = serializers.IntegerField(
        min_value=0, help_text="0-based order of the question to show."
    )


class JoinSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=40, help_text="Display name, 1–40 characters, trimmed.")


class ResponseSubmitSerializer(serializers.Serializer):
    choice_index = serializers.IntegerField(
        min_value=0, required=False, allow_null=True, help_text="MC/TF: 0-based (TF: 0 True, 1 False)."
    )
    text_answer = serializers.CharField(
        max_length=500, required=False, allow_blank=True, help_text="SA: 1–500 characters."
    )

    def validate(self, attrs):
        has_choice = attrs.get("choice_index") is not None
        has_text = bool(attrs.get("text_answer"))
        if has_choice == has_text:
            raise serializers.ValidationError(
                "Send exactly one of choice_index or text_answer.", code="invalid_answer"
            )
        return attrs


# --- Shared -----------------------------------------------------------------


class ActivitySerializer(serializers.ModelSerializer):
    room_id = serializers.IntegerField(read_only=True)
    source_quiz_id = serializers.IntegerField(
        read_only=True,
        allow_null=True,
        help_text="The quiz this was launched from; null for quick questions or once the "
        "quiz is deleted.",
    )

    class Meta:
        model = Activity
        fields = [
            "id",
            "room_id",
            "type",
            "mode",
            "status",
            "quiz_title",
            "source_quiz_id",
            "current_index",
            "show_feedback",
            "shuffle_questions",
            "hide_results",
            "version",
            "started_at",
            "ended_at",
        ]
        read_only_fields = fields


# --- Teacher ----------------------------------------------------------------


class TeacherQuestionSerializer(serializers.ModelSerializer):
    choices = serializers.ListField(
        source="display_choices",
        child=serializers.CharField(),
        help_text='MC options; ["True", "False"] for TF; [] for SA.',
    )

    class Meta:
        model = ActivityQuestion
        fields = [
            "id",
            "order",
            "type",
            "prompt",
            "explanation",
            "choices",
            "correct_index",
            "accepted_answers",
        ]
        read_only_fields = fields


class TeacherParticipantSerializer(serializers.ModelSerializer):
    joined_at = serializers.DateTimeField(source="created_at")

    class Meta:
        model = Participant
        fields = ["id", "name", "joined_at", "finished_at", "last_seen_at"]
        read_only_fields = fields


class TeacherResponseSerializer(serializers.ModelSerializer):
    participant_id = serializers.UUIDField()
    question_id = serializers.IntegerField()

    class Meta:
        model = Response
        fields = [
            "participant_id",
            "question_id",
            "choice_index",
            "text_answer",
            "is_correct",
            "is_locked",
            "submitted_at",
        ]
        read_only_fields = fields


class TextCountSerializer(serializers.Serializer):
    answer = serializers.CharField()
    count = serializers.IntegerField()


class QuestionSummarySerializer(serializers.Serializer):
    question_id = serializers.IntegerField()
    answered_count = serializers.IntegerField()
    correct_count = serializers.IntegerField(
        allow_null=True, help_text="null when the question has no correct answer."
    )
    choice_counts = serializers.ListField(
        child=serializers.IntegerField(), help_text="Count per choice (MC/TF); [] for SA."
    )
    text_counts = TextCountSerializer(
        many=True,
        help_text="SA only: answers grouped trimmed and case-insensitively, most common first.",
    )


class TeacherStateSerializer(serializers.Serializer):
    activity = ActivitySerializer()
    questions = TeacherQuestionSerializer(many=True)
    participants = TeacherParticipantSerializer(
        many=True, help_text="Joined participants, excluding removed ones."
    )
    participant_count = serializers.IntegerField()
    responses = TeacherResponseSerializer(many=True)
    summaries = QuestionSummarySerializer(many=True)


# --- Student ----------------------------------------------------------------


class JoinResultSerializer(serializers.Serializer):
    participant_id = serializers.UUIDField(source="participant.id")
    token = serializers.CharField(
        help_text="Secret for X-Participant-Token. Returned only here: store it."
    )
    activity_id = serializers.IntegerField(source="participant.activity_id")


class FeedbackSerializer(serializers.Serializer):
    is_correct = serializers.BooleanField(allow_null=True)
    correct_index = serializers.IntegerField(allow_null=True)
    accepted_answers = serializers.ListField(child=serializers.CharField())
    explanation = serializers.CharField(allow_blank=True)


class StudentResponseSerializer(serializers.Serializer):
    question_id = serializers.IntegerField(source="response.question_id")
    choice_index = serializers.IntegerField(source="response.choice_index", allow_null=True)
    text_answer = serializers.CharField(source="response.text_answer", allow_blank=True)
    is_locked = serializers.BooleanField(source="response.is_locked")
    submitted_at = serializers.DateTimeField(source="response.submitted_at")
    feedback = FeedbackSerializer(
        allow_null=True,
        help_text="Correctness and explanation; null unless feedback is on and the answer "
        "is locked.",
    )


class StudentQuestionSerializer(serializers.Serializer):
    id = serializers.IntegerField(source="question.id")
    order = serializers.IntegerField(source="question.order")
    type = serializers.ChoiceField(source="question.type", choices=QuestionType.choices)
    prompt = serializers.CharField(source="question.prompt", allow_blank=True)
    choices = serializers.ListField(source="question.display_choices", child=serializers.CharField())
    response = StudentResponseSerializer(allow_null=True, help_text="Your saved answer, or null.")


class StudentActivitySerializer(serializers.ModelSerializer):
    class Meta:
        model = Activity
        fields = [
            "id",
            "type",
            "mode",
            "status",
            "quiz_title",
            "current_index",
            "show_feedback",
            "version",
        ]
        read_only_fields = fields


class StudentParticipantSerializer(serializers.ModelSerializer):
    class Meta:
        model = Participant
        fields = ["id", "name", "finished_at"]
        read_only_fields = fields


class ParticipantStateSerializer(serializers.Serializer):
    activity = StudentActivitySerializer()
    participant = StudentParticipantSerializer()
    question_count = serializers.IntegerField()
    questions = StudentQuestionSerializer(
        many=True,
        help_text="Visible questions: the current one (teacher-paced), all (student-paced), "
        "none once the activity has ended.",
    )
