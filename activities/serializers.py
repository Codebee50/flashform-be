"""Request and response shapes for activities (PRD §8).

Student-facing serializers deliberately have no correct-answer fields: correctness only
reaches students through `FeedbackSerializer`, which `services.feedback_for` fills only
when feedback is on and the student's response is locked.
"""

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
        help_text="QUIZ only, required. TEACHER_PACED: everyone sees the question the "
        "teacher shows. STUDENT_PACED: each student sees every question, can change answers "
        "until they press Finish.",
    )
    show_feedback = serializers.BooleanField(
        default=False,
        help_text="QUIZ only: answers lock on submit, and students then see correct/incorrect "
        "and the explanation for that question.",
    )
    shuffle_questions = serializers.BooleanField(
        default=False,
        help_text="QUIZ, STUDENT_PACED only: each student gets the questions in their own "
        "random order.",
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
        if attrs["shuffle_questions"] and attrs["mode"] != ActivityMode.STUDENT_PACED:
            raise serializers.ValidationError(
                {"shuffle_questions": ["Shuffling is only for student-paced quizzes."]}
            )
        return attrs


class NavigateSerializer(serializers.Serializer):
    index = serializers.IntegerField(
        min_value=0, help_text="0-based order of the question to show."
    )


class ActivityUpdateSerializer(serializers.Serializer):
    hide_results = serializers.BooleanField(
        help_text="Hide the answer distribution on the teacher's live view (for projecting). "
        "Students never see results either way."
    )


class JoinSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=40, help_text="Display name, 1–40 characters, trimmed.")


class ReportListQuerySerializer(serializers.Serializer):
    room = serializers.IntegerField(
        required=False, min_value=1, help_text="Only this room's reports. Omit for all rooms."
    )


class ReportCSVQuerySerializer(serializers.Serializer):
    tz = serializers.CharField(
        required=False,
        default="UTC",
        max_length=64,
        help_text="IANA time zone for joined_at / finished_at, e.g. Europe/London (the "
        "browser's). Default UTC.",
    )

    def validate_tz(self, value):
        try:
            return ZoneInfo(value)
        # OSError: a tzdata directory such as "America" (IsADirectoryError), or a name the
        # filesystem rejects.
        except (ZoneInfoNotFoundError, ValueError, OSError):
            raise serializers.ValidationError("Unknown time zone.")


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
    finished_at = serializers.DateTimeField(
        allow_null=True, help_text="Student-paced: when they pressed Finish; null until then."
    )
    answered_count = serializers.IntegerField(
        help_text="Questions this participant has answered (progress, e.g. 4 of 10)."
    )
    question_count = serializers.IntegerField(help_text="Questions in the activity.")
    score = serializers.IntegerField(
        help_text="Correct answers. Out of the state's total_possible (PRD §17)."
    )

    class Meta:
        model = Participant
        fields = [
            "id",
            "name",
            "joined_at",
            "finished_at",
            "last_seen_at",
            "answered_count",
            "question_count",
            "score",
        ]
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
    total_possible = serializers.IntegerField(
        help_text="Questions with a correct answer: the most a participant can score."
    )
    responses = TeacherResponseSerializer(many=True)
    summaries = QuestionSummarySerializer(many=True)


class ReportRoomSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    code = serializers.CharField()


class ReportSerializer(serializers.Serializer):
    id = serializers.IntegerField(source="activity.id")
    room = ReportRoomSerializer(source="activity.room")
    type = serializers.ChoiceField(source="activity.type", choices=ActivityType.choices)
    mode = serializers.ChoiceField(source="activity.mode", choices=ActivityMode.choices)
    quiz_title = serializers.CharField(
        source="activity.quiz_title", help_text='"" for quick questions.'
    )
    started_at = serializers.DateTimeField(source="activity.started_at")
    ended_at = serializers.DateTimeField(source="activity.ended_at")
    participant_count = serializers.IntegerField(
        help_text="Excludes participants who left while it was live."
    )
    question_count = serializers.IntegerField()
    total_possible = serializers.IntegerField(
        help_text="Questions with a correct answer: the most a participant can score."
    )
    avg_score = serializers.FloatField(
        allow_null=True,
        help_text="Mean correct answers per participant (2 decimals); null without "
        "participants or when total_possible is 0.",
    )
    avg_percent = serializers.FloatField(
        allow_null=True, help_text="avg_score as a percentage of total_possible (1 decimal)."
    )


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
    order = serializers.IntegerField(
        source="question.order",
        help_text="Position in the quiz (0-based). With shuffled questions this is not the "
        "position on the student's screen: number questions by their place in `questions`.",
    )
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
        help_text="Visible questions: the current one (teacher-paced), all (student-paced, "
        "in this student's own order when shuffled), none once the activity has ended.",
    )
