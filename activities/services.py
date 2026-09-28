"""Activity lifecycle, student join, answer submission and the state views (PRD §5.4,
§5.5, §5.6, §7, §8).

Every state change also queues a WebSocket notification (`broadcast`), sent after commit.

Concurrency: every write to an activity runs in one transaction that first locks the
activity row (`_lock_activity`), applies its rules, then bumps `Activity.version`. Writes
to one activity are therefore serialized (a student's double-submit can't race into two
rows), and every state change gets its own version. Starting an activity locks the room
row instead, so two concurrent starts can't both find "no LIVE activity".
"""

import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import timedelta

from django.contrib.auth.models import AbstractBaseUser
from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError

from quizzes.models import Quiz
from quizzes.services import QUESTION_FIELDS
from rooms.models import Room
from rooms.services import get_public_room, normalize_code, owned_rooms

from . import broadcast
from .exceptions import (
    ActivityEnded,
    AlreadyFinished,
    InvalidParticipantToken,
    NoLiveActivity,
    NotCurrentQuestion,
    NotTeacherPaced,
    ResponseLocked,
    RoomLocked,
)
from .models import (
    Activity,
    ActivityMode,
    ActivityQuestion,
    ActivityStatus,
    ActivityType,
    Participant,
    QuestionType,
    Response,
)

DEFAULT_MC_CHOICES = ["A", "B", "C", "D"]  # PRD §17


# --- Results handed to serializers -------------------------------------------


@dataclass
class Feedback:
    """Correctness for one locked response. Only ever built by `feedback_for`."""

    is_correct: bool | None
    correct_index: int | None
    accepted_answers: list[str]
    explanation: str


@dataclass
class ResponseView:
    response: Response
    feedback: Feedback | None


@dataclass
class QuestionView:
    question: ActivityQuestion
    response: ResponseView | None


@dataclass
class ParticipantState:
    activity: Activity
    participant: Participant
    question_count: int
    questions: list[QuestionView]


@dataclass
class JoinResult:
    participant: Participant
    token: str


@dataclass
class TextCount:
    answer: str
    count: int


@dataclass
class QuestionSummary:
    question_id: int
    answered_count: int
    correct_count: int | None
    choice_counts: list[int]
    text_counts: list[TextCount] = field(default_factory=list)


@dataclass
class TeacherState:
    activity: Activity
    questions: list[ActivityQuestion]
    participants: list[Participant]
    responses: list[Response]
    summaries: list[QuestionSummary]

    @property
    def participant_count(self) -> int:
        return len(self.participants)


# --- Helpers ----------------------------------------------------------------


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def normalize_answer(text: str) -> str:
    """SA matching: trimmed, case-insensitive (PRD §17)."""
    return text.strip().casefold()


def _lock_activity(activity_id: int) -> Activity:
    return Activity.objects.select_for_update().get(pk=activity_id)


def _bump_version(activity: Activity, *fields: str) -> None:
    """Save `fields` and increment the version. Call inside the transaction that holds
    the activity's row lock."""
    activity.version += 1
    activity.save(update_fields=["version", "updated_at", *fields])


def _end(activity: Activity) -> None:
    activity.status = ActivityStatus.ENDED
    activity.ended_at = timezone.now()
    Response.objects.filter(question__activity=activity, is_locked=False).update(is_locked=True)
    _bump_version(activity, "status", "ended_at")
    broadcast.activity_ended(activity)


def grade(question: ActivityQuestion, choice_index: int | None = None, text_answer: str = "") -> bool | None:
    """Whether an answer is correct, or None if the question has no correct answer."""
    if question.type == QuestionType.SA:
        if not question.accepted_answers:
            return None
        accepted = {normalize_answer(answer) for answer in question.accepted_answers}
        return normalize_answer(text_answer) in accepted
    if question.correct_index is None:
        return None
    return choice_index == question.correct_index


def feedback_for(activity: Activity, question: ActivityQuestion, response: Response | None) -> Feedback | None:
    """The correct answer for a student, but only once feedback is on and their response
    is locked (PRD §8). This is the only place correct answers reach student payloads."""
    if response is None or not activity.show_feedback or not response.is_locked:
        return None
    return Feedback(
        is_correct=response.is_correct,
        correct_index=question.correct_index,
        accepted_answers=list(question.accepted_answers),
        explanation=question.explanation,
    )


def _clean_answer(question: ActivityQuestion, choice_index: int | None, text_answer: str | None) -> tuple[int | None, str]:
    """Check the answer fits the question type; returns (choice_index, text_answer)."""
    if question.type == QuestionType.SA:
        text = (text_answer or "").strip()
        if choice_index is not None or not text:
            raise ValidationError("Short answer questions need a non-empty text_answer.", code="invalid_answer")
        if len(text) > 500:
            raise ValidationError("Answers can be at most 500 characters.", code="invalid_answer")
        return None, text

    option_count = len(question.display_choices)
    if text_answer or choice_index is None or not 0 <= choice_index < option_count:
        raise ValidationError(
            f"Pick a choice_index from 0 to {option_count - 1}.", code="invalid_answer"
        )
    return choice_index, ""


# --- Teacher ----------------------------------------------------------------


def owned_activities(owner: AbstractBaseUser) -> QuerySet[Activity]:
    """The teacher's activities. Other teachers' are absent, so lookups 404."""
    return Activity.objects.filter(room__owner=owner)


def _launch(
    room: Room,
    *,
    type: str,
    mode: str,
    questions: list[dict],
    show_feedback: bool = False,
    shuffle_questions: bool = False,
    quiz_title: str = "",
    source_quiz=None,
) -> Activity:
    """End the room's LIVE activity (if any) and start a new one with snapshotted
    questions, in one transaction."""
    with transaction.atomic():
        Room.objects.select_for_update().get(pk=room.pk)
        for live in Activity.objects.select_for_update().filter(room=room, status=ActivityStatus.LIVE):
            _end(live)
        activity = Activity.objects.create(
            room=room,
            type=type,
            mode=mode,
            show_feedback=show_feedback,
            shuffle_questions=shuffle_questions,
            quiz_title=quiz_title,
            source_quiz=source_quiz,
        )
        ActivityQuestion.objects.bulk_create(
            ActivityQuestion(activity=activity, order=order, **question)
            for order, question in enumerate(questions)
        )
        broadcast.activity_started(activity)
    return activity


def start_quick_activity(room: Room, *, type: str, prompt: str = "", choices: list[str] | None = None) -> Activity:
    """Start a Quick Question (PRD QQ1–QQ3). MC defaults to options A–D; TF and SA have
    no choices. Quick questions have no correct answer."""
    question = {"type": type, "prompt": prompt}
    if type == QuestionType.MC:
        question["choices"] = list(choices) if choices else list(DEFAULT_MC_CHOICES)
    return _launch(
        room, type=ActivityType.QUICK, mode=ActivityMode.TEACHER_PACED, questions=[question]
    )


def start_quiz_activity(
    room: Room,
    quiz_id: int,
    *,
    mode: str,
    show_feedback: bool = False,
    shuffle_questions: bool = False,
) -> Activity:
    """Start a saved quiz in the room (PRD A1). Its title and questions are copied into
    the activity, so editing or deleting the quiz later changes neither this run nor its
    report (PRD Q6, §7).

    404 `quiz_not_found` if the quiz doesn't exist or belongs to another teacher; the
    room's LIVE activity is left running in that case.
    """
    with transaction.atomic():
        # Lock the quiz so a concurrent save can't replace its questions mid-copy.
        quiz = Quiz.objects.select_for_update().filter(pk=quiz_id, owner_id=room.owner_id).first()
        if quiz is None:
            raise NotFound("Quiz not found.", code="quiz_not_found")
        questions = [
            {name: getattr(question, name) for name in QUESTION_FIELDS}
            for question in quiz.questions.order_by("order")
        ]
        if not questions:
            raise ValidationError("This quiz has no questions.", code="quiz_empty")
        return _launch(
            room,
            type=ActivityType.QUIZ,
            mode=mode,
            questions=questions,
            show_feedback=show_feedback,
            shuffle_questions=shuffle_questions,
            quiz_title=quiz.title,
            source_quiz=quiz,
        )


def navigate(activity: Activity, index: int) -> Activity:
    """Move a teacher-paced activity to question `index` (Next / Previous, PRD A1).
    Responses to the question being left are locked (PRD §17); students who hadn't
    answered it can still do so if the teacher comes back to it.

    Idempotent: navigating to the current question changes nothing (no version bump, no
    event), so retrying is safe. 400 `invalid_index`; 409 `activity_ended` or
    `not_teacher_paced`.
    """
    with transaction.atomic():
        activity = _lock_activity(activity.pk)
        if not activity.is_live:
            raise ActivityEnded()
        if activity.mode != ActivityMode.TEACHER_PACED:
            raise NotTeacherPaced()
        count = activity.questions.count()
        if not 0 <= index < count:
            raise ValidationError(f"Pick an index from 0 to {count - 1}.", code="invalid_index")
        if index != activity.current_index:
            Response.objects.filter(
                question__activity=activity,
                question__order=activity.current_index,
                is_locked=False,
            ).update(is_locked=True)
            activity.current_index = index
            _bump_version(activity, "current_index")
            broadcast.activity_updated(activity)
    return activity


def end_activity(activity: Activity) -> Activity:
    """End a LIVE activity and lock its responses. Ending an ended activity is a no-op."""
    with transaction.atomic():
        activity = _lock_activity(activity.pk)
        if activity.is_live:
            _end(activity)
    return activity


def get_teacher_state(activity: Activity) -> TeacherState:
    """Everything the teacher's live view needs, in a fixed number of queries."""
    activity = Activity.objects.get(pk=activity.pk)
    questions = list(activity.questions.all())
    participants = list(activity.participants.filter(is_removed=False))
    responses = list(
        Response.objects.filter(participant__activity=activity, participant__is_removed=False)
    )

    by_question: dict[int, list[Response]] = {question.id: [] for question in questions}
    for response in responses:
        by_question[response.question_id].append(response)

    summaries = []
    for question in questions:
        answers = by_question[question.id]
        choice_counts = [0] * len(question.display_choices)
        texts: dict[str, TextCount] = {}
        for response in answers:
            if response.choice_index is not None and response.choice_index < len(choice_counts):
                choice_counts[response.choice_index] += 1
            if question.type == QuestionType.SA:
                key = normalize_answer(response.text_answer)
                texts.setdefault(key, TextCount(answer=response.text_answer, count=0)).count += 1
        summaries.append(
            QuestionSummary(
                question_id=question.id,
                answered_count=len(answers),
                correct_count=(
                    sum(1 for response in answers if response.is_correct)
                    if question.has_correct_answer
                    else None
                ),
                choice_counts=choice_counts,
                text_counts=sorted(texts.values(), key=lambda text: (-text.count, text.answer)),
            )
        )

    return TeacherState(
        activity=activity,
        questions=questions,
        participants=participants,
        responses=responses,
        summaries=summaries,
    )


# --- Student ----------------------------------------------------------------


def join_room(code: str, name: str) -> JoinResult:
    """Join the room's LIVE activity as a new participant (PRD S1).

    404 `room_not_found`, 423 `room_locked`, 409 `no_live_activity`. The raw token is only
    returned here; only its sha256 is stored.
    """
    room = get_public_room(code)
    if room.is_locked:
        raise RoomLocked()

    token = secrets.token_urlsafe(32)
    with transaction.atomic():
        activity = (
            Activity.objects.select_for_update().filter(room=room, status=ActivityStatus.LIVE).first()
        )
        if activity is None:
            raise NoLiveActivity()
        participant = Participant.objects.create(
            activity=activity, name=name.strip(), token_hash=hash_token(token)
        )
        _bump_version(activity)
        broadcast.participants_changed(activity)
    return JoinResult(participant=participant, token=token)


def authenticate_participant(token: str | None) -> Participant:
    """The participant for an `X-Participant-Token`, or 401. Removed participants
    (teacher removed them, or they left) no longer authenticate."""
    if not token:
        raise InvalidParticipantToken()
    try:
        return Participant.objects.select_related("activity").get(
            token_hash=hash_token(token), is_removed=False
        )
    except Participant.DoesNotExist:
        raise InvalidParticipantToken()


def _visible_questions(activity: Activity, participant: Participant) -> list[ActivityQuestion]:
    """What the student may see: nothing once ended, the current question when
    teacher-paced, every question (in their own order if shuffled) when student-paced."""
    if not activity.is_live:
        return []
    questions = list(activity.questions.all())
    if activity.mode == ActivityMode.TEACHER_PACED:
        return [question for question in questions if question.order == activity.current_index]
    if participant.question_order:
        by_order = {question.order: question for question in questions}
        return [by_order[order] for order in participant.question_order if order in by_order]
    return questions


def get_participant_state(participant: Participant) -> ParticipantState:
    """Everything the student screen needs (PRD §8). Never includes correct answers
    unless feedback is on and this student's response to that question is locked."""
    activity = Activity.objects.get(pk=participant.activity_id)
    questions = _visible_questions(activity, participant)
    responses = {
        response.question_id: response
        for response in participant.responses.filter(question__in=questions)
    }
    views = []
    for question in questions:
        response = responses.get(question.id)
        views.append(
            QuestionView(
                question=question,
                response=(
                    ResponseView(response, feedback_for(activity, question, response))
                    if response
                    else None
                ),
            )
        )
    return ParticipantState(
        activity=activity,
        participant=participant,
        question_count=activity.questions.count(),
        questions=views,
    )


def submit_response(
    participant: Participant,
    question_id: int,
    *,
    choice_index: int | None = None,
    text_answer: str | None = None,
) -> ResponseView:
    """Save (upsert) a participant's answer and grade it. Safe to retry.

    404 if the question isn't in the participant's activity; 409 if the activity ended,
    the question isn't current (teacher-paced), the student finished (student-paced) or
    the response is locked; 400 `invalid_answer` if the answer doesn't fit the question.
    """
    with transaction.atomic():
        activity = _lock_activity(participant.activity_id)
        try:
            question = activity.questions.get(pk=question_id)
        except ActivityQuestion.DoesNotExist:
            raise NotFound("Question not found.", code="question_not_found")

        # Re-read under the lock: the student may have left or finished meanwhile.
        participant.refresh_from_db(fields=["is_removed", "finished_at"])
        if participant.is_removed:
            raise InvalidParticipantToken()
        if not activity.is_live:
            raise ActivityEnded()
        if activity.mode == ActivityMode.TEACHER_PACED and question.order != activity.current_index:
            raise NotCurrentQuestion()
        if activity.mode == ActivityMode.STUDENT_PACED and participant.finished_at:
            raise AlreadyFinished()

        response = Response.objects.filter(participant=participant, question=question).first()
        if response is not None and response.is_locked:
            raise ResponseLocked()

        choice_index, text_answer = _clean_answer(question, choice_index, text_answer)
        values = {
            "choice_index": choice_index,
            "text_answer": text_answer,
            "is_correct": grade(question, choice_index, text_answer),
            # With feedback on, the student sees the answer now, so it can't change (A2).
            "is_locked": activity.show_feedback,
            "submitted_at": timezone.now(),
        }
        if response is None:
            response = Response.objects.create(participant=participant, question=question, **values)
        else:
            for name, value in values.items():
                setattr(response, name, value)
            response.save(update_fields=[*values, "updated_at"])
        _bump_version(activity)
        broadcast.responses_updated(activity)

    return ResponseView(response, feedback_for(activity, question, response))


def leave(participant: Participant) -> None:
    """The student leaves (PRD S6): their token stops working; their answers are kept."""
    with transaction.atomic():
        activity = _lock_activity(participant.activity_id)
        participant.is_removed = True
        participant.save(update_fields=["is_removed", "updated_at"])
        _bump_version(activity)
        broadcast.participants_changed(activity)


# --- WebSockets -------------------------------------------------------------

LAST_SEEN_INTERVAL = timedelta(seconds=30)


def get_room_by_code(code: str) -> Room | None:
    return Room.objects.filter(code=normalize_code(code)).first()


def get_owned_room(owner: AbstractBaseUser, room_id: int) -> Room | None:
    return owned_rooms(owner).filter(pk=room_id).first()


def participant_in_room(token: str, room: Room) -> Participant | None:
    """The (not removed) participant for a token, if it belongs to an activity in `room`.
    Tokens from earlier activities in the room still count: the waiting screen keeps one."""
    return Participant.objects.filter(
        token_hash=hash_token(token), is_removed=False, activity__room=room
    ).first()


def touch_participant(participant_id) -> bool:
    """Record a heartbeat, writing `last_seen_at` at most once per 30 s (PRD §9). One
    conditional UPDATE, so several sockets of one student can't write more often. Not a
    state change, so no version bump. Returns whether it wrote."""
    now = timezone.now()
    return bool(
        Participant.objects.filter(
            pk=participant_id, last_seen_at__lt=now - LAST_SEEN_INTERVAL
        ).update(last_seen_at=now)
    )
