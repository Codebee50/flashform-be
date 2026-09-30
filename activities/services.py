"""Activity lifecycle, student join, answer submission and the state views (PRD §5.4,
§5.5, §5.6, §7, §8).

Every state change also queues a WebSocket notification (`broadcast`), sent after commit.

Concurrency: every write to an activity runs in one transaction that first locks the
activity row (`_lock_activity`), applies its rules, then bumps `Activity.version`. Writes
to one activity are therefore serialized (a student's double-submit can't race into two
rows), and every state change gets its own version. Starting an activity locks the room
row instead, so two concurrent starts can't both find "no LIVE activity".
"""

import csv
import hashlib
import io
import random
import secrets
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo

from django.contrib.auth.models import AbstractBaseUser
from django.db import transaction
from django.db.models import Count, QuerySet
from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError

from quizzes.models import Quiz
from quizzes.services import QUESTION_FIELDS
from rooms.models import Room
from rooms.services import get_public_room, normalize_code, owned_rooms

from . import broadcast
from .exceptions import (
    ActivityEnded,
    ActivityLive,
    AlreadyFinished,
    InvalidParticipantToken,
    NoLiveActivity,
    NotCurrentQuestion,
    NotEnoughAnswers,
    NotShortAnswer,
    NotStudentPaced,
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

    @property
    def total_possible(self) -> int:
        return total_possible(self.questions)


@dataclass
class ReportRow:
    """One ENDED activity in the reports list (PRD RP1)."""

    activity: Activity
    participant_count: int
    question_count: int
    total_possible: int
    correct_count: int  # correct responses of all listed participants together

    @property
    def avg_score(self) -> float | None:
        """Mean score per participant; None without participants or gradable questions."""
        if not self.participant_count or not self.total_possible:
            return None
        return round(self.correct_count / self.participant_count, 2)

    @property
    def avg_percent(self) -> float | None:
        if not self.participant_count:
            return None
        return percent(self.correct_count / self.participant_count, self.total_possible)


# --- Helpers ----------------------------------------------------------------


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def normalize_answer(text: str) -> str:
    """SA matching: trimmed, case-insensitive (PRD §17)."""
    return text.strip().casefold()


def total_possible(questions) -> int:
    """Questions without a correct answer don't count towards the score (PRD §17)."""
    return sum(1 for question in questions if question.has_correct_answer)


def percent(score: float, total: int) -> float | None:
    return round(score / total * 100, 1) if total else None


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


def start_vote(activity: Activity) -> Activity:
    """Turn a LIVE short answer quick question into a vote (PRD QQ4): its distinct answers,
    trimmed and case-insensitive (PRD §17), become the options of a new quick MC question
    with the same prompt, which replaces it as the room's LIVE activity. Each option is
    spelled as its earliest submission, and options are in submission order. Answers of
    removed participants (or ones who left) don't count.

    Only works while the SA question is LIVE, so a retry after the vote started (which
    ended it) is a 409 `activity_ended`, never a second vote. 409 `not_short_answer` for
    any other activity; 409 `not_enough_answers` with fewer than 2 distinct answers.
    """
    with transaction.atomic():
        # Room before activity, the order `_launch` locks them in, so a concurrent start
        # can't deadlock with us.
        room = Room.objects.select_for_update().get(pk=activity.room_id)
        activity = _lock_activity(activity.pk)
        question = activity.questions.first()
        if activity.type != ActivityType.QUICK or question.type != QuestionType.SA:
            raise NotShortAnswer()
        if not activity.is_live:
            raise ActivityEnded()

        answers = (
            Response.objects.filter(question=question, participant__is_removed=False)
            .order_by("submitted_at", "id")
            .values_list("text_answer", flat=True)
        )
        choices: dict[str, str] = {}
        for text in answers:
            choices.setdefault(normalize_answer(text), text)
        if len(choices) < 2:
            raise NotEnoughAnswers()

        vote = {"type": QuestionType.MC, "prompt": question.prompt, "choices": list(choices.values())}
        return _launch(
            room, type=ActivityType.QUICK, mode=ActivityMode.TEACHER_PACED, questions=[vote]
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


def update_activity(activity: Activity, *, hide_results: bool) -> Activity:
    """Change the teacher's display settings (PRD L4: Hide results). Stored on the activity
    so every teacher tab (laptop and projector) agrees and a reload keeps it. Allowed on
    ENDED activities too (the report is projected the same way).

    Students never see results, so only the teacher is notified. Idempotent: setting the
    current value changes nothing (no version bump, no event).
    """
    with transaction.atomic():
        activity = _lock_activity(activity.pk)
        if activity.hide_results != hide_results:
            activity.hide_results = hide_results
            _bump_version(activity, "hide_results")
            broadcast.activity_updated(activity, students=False)
    return activity


def remove_participant(activity: Activity, participant_id) -> None:
    """The teacher removes a participant (PRD L5): their token stops working, they drop out
    of teacher-state and the report (answers are kept in the DB), and their screen goes
    back to the join screen (`participant_removed`). They may join again unless the room
    is locked: their old token no longer lets them past the lock.

    Works on ENDED activities too, so a student idling on the waiting screen can be
    removed. Idempotent: removing a removed (or departed) participant changes nothing.
    404 `participant_not_found` if they aren't in this activity.
    """
    with transaction.atomic():
        activity = _lock_activity(activity.pk)
        participant = activity.participants.filter(pk=participant_id).first()
        if participant is None:
            raise NotFound("Participant not found.", code="participant_not_found")
        if participant.is_removed:
            return
        participant.is_removed = True
        participant.save(update_fields=["is_removed", "updated_at"])
        _bump_version(activity)
        broadcast.participant_removed(activity, participant.id)


def get_teacher_state(activity: Activity) -> TeacherState:
    """Everything the teacher's live view needs, in a fixed number of queries.

    Each participant also gets `answered_count` and `question_count` (their progress,
    e.g. "4/10", PRD L3) and `score` (correct answers, PRD §17), set like query
    annotations. For an ENDED activity this is also the report detail (PRD RP2).
    """
    activity = Activity.objects.get(pk=activity.pk)
    questions = list(activity.questions.all())
    participants = list(activity.participants.filter(is_removed=False))
    responses = list(
        Response.objects.filter(participant__activity=activity, participant__is_removed=False)
    )

    answered = Counter(response.participant_id for response in responses)
    correct = Counter(response.participant_id for response in responses if response.is_correct)
    for participant in participants:
        participant.answered_count = answered[participant.id]
        participant.question_count = len(questions)
        participant.score = correct[participant.id]

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


# --- Reports ----------------------------------------------------------------
#
# A report covers the same participants as teacher-state (not the removed/left ones), so
# the list, the detail page (teacher-state) and the CSV always agree.

CSV_PROMPT_LENGTH = 60  # PRD RP3
CSV_COLUMNS = ["name", "joined_at", "finished_at", "score", "total_possible", "percent"]
# Spreadsheets run cells starting with these as formulas (CSV injection). Names and
# answers come from students, so such cells get a leading apostrophe.
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def list_reports(owner: AbstractBaseUser, room: Room | None = None) -> list[ReportRow]:
    """The teacher's ENDED activities, newest first, optionally for one room (PRD RP1).
    Four queries however many activities there are."""
    activities = owned_activities(owner).filter(status=ActivityStatus.ENDED).select_related("room")
    if room is not None:
        activities = activities.filter(room=room)
    activities = list(activities)
    ids = [activity.id for activity in activities]

    participant_counts = dict(
        Participant.objects.filter(activity_id__in=ids, is_removed=False)
        .order_by()
        .values_list("activity_id")
        .annotate(Count("id"))
    )
    correct_counts = dict(
        Response.objects.filter(
            participant__activity_id__in=ids, participant__is_removed=False, is_correct=True
        )
        .order_by()
        .values_list("participant__activity_id")
        .annotate(Count("id"))
    )
    questions: dict[int, list[ActivityQuestion]] = {activity_id: [] for activity_id in ids}
    for question in ActivityQuestion.objects.filter(activity_id__in=ids).only(
        "activity_id", "type", "correct_index", "accepted_answers"
    ):
        questions[question.activity_id].append(question)

    return [
        ReportRow(
            activity=activity,
            participant_count=participant_counts.get(activity.id, 0),
            question_count=len(questions[activity.id]),
            total_possible=total_possible(questions[activity.id]),
            correct_count=correct_counts.get(activity.id, 0),
        )
        for activity in activities
    ]


def _csv_cell(value: str) -> str:
    return f"'{value}" if value.startswith(FORMULA_PREFIXES) else value


def _csv_header(question: ActivityQuestion) -> str:
    """`Q3: <prompt>`, the prompt on one line and truncated to 60 characters (PRD RP3)."""
    prompt = " ".join(question.prompt.split())
    if len(prompt) > CSV_PROMPT_LENGTH:
        prompt = prompt[: CSV_PROMPT_LENGTH - 1].rstrip() + "…"
    label = f"Q{question.order + 1}"
    return f"{label}: {prompt}" if prompt else label


def _answer_text(question: ActivityQuestion, response: Response | None) -> str:
    """The chosen option's text (MC/TF) or the typed answer (SA); blank if unanswered."""
    if response is None:
        return ""
    choices = question.display_choices
    if response.choice_index is not None and response.choice_index < len(choices):
        return choices[response.choice_index]
    return response.text_answer


def distinct_names(participants: list[Participant]) -> list[str]:
    """Names in join order, repeats suffixed `(2)`, `(3)`… as in the teacher UI (PRD S2)."""
    seen: Counter[str] = Counter()
    names = []
    for participant in participants:
        seen[participant.name] += 1
        count = seen[participant.name]
        names.append(participant.name if count == 1 else f"{participant.name} ({count})")
    return names


def report_csv(activity: Activity, tz: tzinfo) -> str:
    """The report as CSV (PRD RP3): one row per participant, then one column per question
    with the answer text. Times are `YYYY-MM-DD HH:MM:SS` in `tz`."""

    def when(moment: datetime | None) -> str:
        return moment.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S") if moment else ""

    state = get_teacher_state(activity)
    responses = {
        (response.participant_id, response.question_id): response for response in state.responses
    }
    total = state.total_possible

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(CSV_COLUMNS + [_csv_cell(_csv_header(question)) for question in state.questions])
    for participant, name in zip(state.participants, distinct_names(state.participants)):
        score_percent = percent(participant.score, total)
        writer.writerow(
            [
                _csv_cell(name),
                when(participant.created_at),
                when(participant.finished_at),
                participant.score,
                total,
                "" if score_percent is None else score_percent,
            ]
            + [
                _csv_cell(_answer_text(question, responses.get((participant.id, question.id))))
                for question in state.questions
            ]
        )
    return out.getvalue()


def report_filename(activity: Activity) -> str:
    return f"report-{activity.room.code}-{activity.started_at:%Y-%m-%d}-{activity.id}.csv"


def delete_report(activity: Activity) -> None:
    """Delete an ENDED activity with its questions, participants and responses (PRD RP4).
    409 `activity_live` while it is still running: end it first. ENDED is final, so the
    check can't race with the activity going live again."""
    if activity.is_live:
        raise ActivityLive()
    activity.delete()


# --- Student ----------------------------------------------------------------


def join_room(code: str, name: str, token: str | None = None) -> JoinResult:
    """Join the room's LIVE activity as a new participant (PRD S1).

    A locked room refuses new students (PRD R4), but students who already joined are
    unaffected: `token`, the participant token from an earlier (or the current) activity
    in this room, lets them re-join the next activity with their saved name (PRD §7).
    Tokens of students who left or were removed don't count.

    404 `room_not_found`, 423 `room_locked`, 409 `no_live_activity`. The raw token is only
    returned here; only its sha256 is stored.
    """
    room = get_public_room(code)
    if room.is_locked and not (token and participant_in_room(token, room)):
        raise RoomLocked()

    token = secrets.token_urlsafe(32)
    with transaction.atomic():
        activity = (
            Activity.objects.select_for_update().filter(room=room, status=ActivityStatus.LIVE).first()
        )
        if activity is None:
            raise NoLiveActivity()
        question_order = None
        if activity.shuffle_questions:
            # PRD A3: each student gets their own order, fixed for the whole activity.
            orders = list(activity.questions.values_list("order", flat=True))
            question_order = random.sample(orders, len(orders))
        participant = Participant.objects.create(
            activity=activity,
            name=name.strip(),
            token_hash=hash_token(token),
            question_order=question_order,
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


def finish(participant: Participant) -> ParticipantState:
    """The student presses Finish in a student-paced activity (PRD A1): every answer they
    gave is locked, and they can't answer anything else. Unanswered questions stay
    unanswered.

    Idempotent: finishing again changes nothing (no version bump, no event), so retrying
    is safe. 409 `activity_ended` or `not_student_paced`.
    """
    with transaction.atomic():
        activity = _lock_activity(participant.activity_id)
        participant.refresh_from_db(fields=["is_removed", "finished_at"])
        if participant.is_removed:
            raise InvalidParticipantToken()
        if not activity.is_live:
            raise ActivityEnded()
        if activity.mode != ActivityMode.STUDENT_PACED:
            raise NotStudentPaced()
        if participant.finished_at is None:
            participant.finished_at = timezone.now()
            participant.save(update_fields=["finished_at", "updated_at"])
            participant.responses.filter(is_locked=False).update(is_locked=True)
            _bump_version(activity)
            broadcast.participants_changed(activity)
    return get_participant_state(participant)


def leave(participant: Participant) -> None:
    """The student leaves (PRD S6): their token stops working; their answers are kept.

    Leaving an ENDED activity (the usual case: "Leave room" on the waiting screen after
    class) changes nothing, so the student stays in that activity's report.
    """
    with transaction.atomic():
        activity = _lock_activity(participant.activity_id)
        if not activity.is_live:
            return
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
