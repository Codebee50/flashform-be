"""PRD §11 security, checked across the whole API instead of feature by feature.

- Route inventory: every /api route is either public (PUBLIC_ROUTES), about the caller
  only (SELF_ROUTES), a list (LIST_ROUTES) or has an ownership case in OWNED_CASES. A new
  endpoint fails `test_every_api_route_is_classified` until it is added to one of them.
- Every teacher route: 401 without a teacher JWT, 404 for another teacher's object, and
  nothing changes in either case.
- Every text input has a server-side length limit, and oversized bodies are a JSON 413.
- Nothing a student can reach (REST or WebSocket) carries a correct answer before their
  own response is locked with feedback on, or another student's answer.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass

import pytest
from django.urls import URLResolver, get_resolver
from rest_framework.permissions import AllowAny
from rest_framework.test import APIClient

from activities import services
from activities.models import Activity, ActivityMode, Participant, Response
from quizzes.models import Quiz, QuizQuestion
from quizzes.services import create_quiz
from rooms.models import Room
from tests.test_activities import (  # noqa: F401 (fixtures)
    ANSWER_KEYS,
    STATE,
    join_url,
    keys_in,
    launch,
    other_client,
    other_teacher,
    response_url,
    room,
)
from tests.test_live_controls import payloads, sent  # noqa: F401 (fixtures)

pytestmark = pytest.mark.django_db

FINISH = "/api/participant/finish"


# --- The world: one teacher's objects, plus another teacher ------------------


def full_question(**fields) -> dict:
    """A question with every QuizQuestion field, as `create_quiz` expects."""
    return {"explanation": "", "choices": [], "correct_index": None, "accepted_answers": [], **fields}


QUIZ_QUESTIONS = [
    full_question(type="MC", prompt="Capital of France?", choices=["Berlin", "Paris"], correct_index=1),
    full_question(type="TF", prompt="2 + 2 = 5", correct_index=1),
]


@dataclass
class World:
    room: Room  # holds `live` (teacher-paced quiz) and `ended` (a quick TF question)
    vote_room: Room  # holds `sa`, a LIVE short answer quick question with 2 answers
    other_room: Room  # belongs to the other teacher
    quiz: Quiz
    live: Activity
    ended: Activity
    sa: Activity
    participant: Participant  # in `live`, has answered question 0
    token: str  # `participant`'s token
    sa_token: str  # a participant of `sa`


def joined(code: str, name: str) -> services.JoinResult:
    return services.join_room(code, name)


@pytest.fixture
def world(teacher, other_teacher) -> World:
    room = Room.objects.create(owner=teacher, name="Period 3 Biology", code="BIO3AB")
    vote_room = Room.objects.create(owner=teacher, name="Period 5 Physics", code="PHYS5X")
    other_room = Room.objects.create(owner=other_teacher, name="Bob's room", code="BOBRM7")
    quiz = create_quiz(owner=teacher, title="Week 1", questions=QUIZ_QUESTIONS)

    ended = services.start_quick_activity(room, type="TF")
    services.submit_response(joined("BIO3AB", "Ada").participant, ended.questions.get().id, choice_index=0)
    services.end_activity(ended)

    live = services.start_quiz_activity(room, quiz.id, mode=ActivityMode.TEACHER_PACED)
    student = joined("BIO3AB", "Grace")
    services.submit_response(student.participant, live.questions.get(order=0).id, choice_index=1)

    sa = services.start_quick_activity(vote_room, type="SA")
    voters = [joined("PHYS5X", name) for name in ("Alan", "Edsger")]
    for voter, text in zip(voters, ["yes", "no"]):
        services.submit_response(voter.participant, sa.questions.get().id, text_answer=text)

    return World(
        room=room,
        vote_room=vote_room,
        other_room=other_room,
        quiz=quiz,
        live=live,
        ended=ended,
        sa=sa,
        participant=student.participant,
        token=student.token,
        sa_token=voters[0].token,
    )


def snapshot() -> tuple:
    """Everything a teacher endpoint could change."""
    return (
        list(Room.objects.order_by("id").values("id", "owner_id", "name", "code", "is_locked")),
        list(Quiz.objects.order_by("id").values("id", "owner_id", "title")),
        list(QuizQuestion.objects.order_by("id").values("id", "quiz_id", "order", "prompt")),
        list(
            Activity.objects.order_by("id").values(
                "id", "room_id", "status", "version", "current_index", "hide_results"
            )
        ),
        list(Participant.objects.order_by("id").values("id", "is_removed", "finished_at")),
        list(Response.objects.order_by("id").values("id", "choice_index", "text_answer", "is_locked")),
    )


# --- Route inventory --------------------------------------------------------


@dataclass
class Case:
    """One teacher request on an object owned by `teacher`."""

    route: str  # URL name
    method: str
    path: Callable[[World], str]
    body: dict | None = None
    ok: int = 200  # the owner's status code

    def call(self, client: APIClient, world: World):
        return getattr(client, self.method)(self.path(world), self.body, format="json")

    def __str__(self) -> str:
        return f"{self.method.upper()} {self.route}"


OWNED_CASES = [
    Case("room-detail", "get", lambda w: f"/api/rooms/{w.room.id}"),
    Case("room-detail", "patch", lambda w: f"/api/rooms/{w.room.id}", {"name": "Mine", "is_locked": True}),
    Case("room-detail", "delete", lambda w: f"/api/rooms/{w.room.id}", ok=204),
    Case("quiz-detail", "get", lambda w: f"/api/quizzes/{w.quiz.id}"),
    Case(
        "quiz-detail",
        "put",
        lambda w: f"/api/quizzes/{w.quiz.id}",
        {"title": "Mine", "questions": [{"type": "TF", "prompt": "Mine?"}]},
    ),
    Case("quiz-detail", "delete", lambda w: f"/api/quizzes/{w.quiz.id}", ok=204),
    Case("quiz-duplicate", "post", lambda w: f"/api/quizzes/{w.quiz.id}/duplicate", ok=201),
    Case(
        "activity-create",
        "post",
        lambda w: f"/api/rooms/{w.room.id}/activities",
        {"type": "QUICK", "question": {"type": "MC"}},
        ok=201,
    ),
    Case("report-list", "get", lambda w: f"/api/activities?room={w.room.id}"),
    Case("activity-detail", "patch", lambda w: f"/api/activities/{w.live.id}", {"hide_results": True}),
    Case("activity-detail", "delete", lambda w: f"/api/activities/{w.ended.id}", ok=204),
    Case("activity-report-csv", "get", lambda w: f"/api/activities/{w.ended.id}/report.csv"),
    Case("activity-teacher-state", "get", lambda w: f"/api/activities/{w.live.id}/teacher-state"),
    Case("activity-navigate", "post", lambda w: f"/api/activities/{w.live.id}/navigate", {"index": 1}),
    Case("activity-end", "post", lambda w: f"/api/activities/{w.live.id}/end"),
    Case("activity-vote", "post", lambda w: f"/api/activities/{w.sa.id}/vote", ok=201),
    Case(
        "activity-participant-remove",
        "delete",
        lambda w: f"/api/activities/{w.live.id}/participants/{w.participant.id}",
        ok=204,
    ),
]

# No teacher auth (PRD §8 student endpoints, auth, health, API docs).
PUBLIC_ROUTES = {
    "health",
    "schema",
    "swagger-ui",
    "auth-register",
    "auth-login",
    "auth-refresh",
    "auth-logout",
    "auth-verify-email",
    "auth-resend-verification",
    "auth-password-reset",
    "auth-password-reset-confirm",
    "room-public",
    "room-join",
    "participant-state",
    "participant-response",
    "participant-finish",
    "participant-leave",
}
# Authenticated, but only ever about the caller: there is no object id to swap.
SELF_ROUTES = {"auth-me"}
# Listing endpoints (their POST, if any, creates for the caller): see
# test_lists_show_only_your_own_objects.
LIST_ROUTES = {"room-list", "quiz-list", "report-list"}


def api_routes() -> dict[str, Callable]:
    """Every /api URL name, with its view."""
    routes = {}

    def walk(patterns, prefix: str) -> None:
        for pattern in patterns:
            route = prefix + str(pattern.pattern)
            if isinstance(pattern, URLResolver):
                walk(pattern.url_patterns, route)
            elif route.startswith("api/"):
                routes[pattern.name or route] = pattern.callback

    walk(get_resolver().url_patterns, "")
    return routes


def test_every_api_route_is_classified():
    classified = PUBLIC_ROUTES | SELF_ROUTES | LIST_ROUTES | {case.route for case in OWNED_CASES}

    assert set(api_routes()) == classified


def test_only_public_routes_allow_anonymous_access():
    for name, view in api_routes().items():
        anonymous = any(
            permission is AllowAny for permission in view.cls.permission_classes
        )
        assert anonymous == (name in PUBLIC_ROUTES), name


# --- Teacher ownership (every object → 404 for another teacher) --------------


@pytest.mark.parametrize("case", OWNED_CASES, ids=str)
def test_another_teacher_gets_404_and_changes_nothing(world, other_client, case):
    before = snapshot()

    response = case.call(other_client, world)

    assert response.status_code == 404, response.content
    assert response["Content-Type"] == "application/json"
    assert response.json()["code"] == "not_found"
    assert snapshot() == before


@pytest.mark.parametrize("credentials", ["none", "garbage-jwt", "participant-token"])
@pytest.mark.parametrize("case", OWNED_CASES, ids=str)
def test_teacher_routes_need_a_teacher_jwt(world, case, credentials):
    client = APIClient()
    if credentials == "garbage-jwt":
        client.credentials(HTTP_AUTHORIZATION="Bearer not-a-jwt")
    elif credentials == "participant-token":
        client.credentials(HTTP_X_PARTICIPANT_TOKEN=world.token)
    before = snapshot()

    response = case.call(client, world)

    assert response.status_code == 401, response.content
    assert snapshot() == before


@pytest.mark.parametrize("case", OWNED_CASES, ids=str)
def test_the_owner_passes_every_ownership_case(world, teacher_client, case):
    """Keeps the two tests above honest: a mistyped path would 404 for everyone."""
    response = case.call(teacher_client, world)

    assert response.status_code == case.ok, response.content


def test_lists_show_only_your_own_objects(world, teacher_client, other_client):
    assert [room["id"] for room in other_client.get("/api/rooms").json()] == [world.other_room.id]
    assert other_client.get("/api/quizzes").json() == []
    assert other_client.get("/api/activities").json() == []

    # The owner does see them, so the empty lists above mean something.
    mine = {room["id"] for room in teacher_client.get("/api/rooms").json()}
    assert mine == {world.room.id, world.vote_room.id}
    assert [quiz["id"] for quiz in teacher_client.get("/api/quizzes").json()] == [world.quiz.id]
    assert [report["id"] for report in teacher_client.get("/api/activities").json()] == [world.ended.id]


def test_a_teacher_cannot_launch_another_teachers_quiz_in_their_own_room(world, other_client):
    before = snapshot()

    response = other_client.post(
        f"/api/rooms/{world.other_room.id}/activities",
        {"type": "QUIZ", "quiz_id": world.quiz.id, "mode": "TEACHER_PACED"},
        format="json",
    )

    assert response.status_code == 404
    assert response.json()["code"] == "quiz_not_found"
    assert snapshot() == before


# --- Input length limits ----------------------------------------------------


PASSWORD = "correct-horse-battery"


def long(length: int) -> str:
    return "x" * length


def email(length: int) -> str:
    """A syntactically valid address of exactly `length` characters."""
    domain = "@example.com"
    return "a" * (length - len(domain)) + domain


def quiz_with(**question) -> dict:
    return {"title": "T", "questions": [{"type": "MC", "prompt": "Q?", "choices": ["A", "B"], **question}]}


def quick(**question) -> dict:
    return {"type": "QUICK", "question": {"type": "MC", **question}}


LENGTH_CASES = [
    # (client, method, path, body, field reported in `fields`)
    pytest.param("anon", "post", "/api/auth/register", {"name": long(151), "email": email(20), "password": PASSWORD}, "name", id="register-name"),
    pytest.param("anon", "post", "/api/auth/register", {"name": "A", "email": email(151), "password": PASSWORD}, "email", id="register-email"),
    pytest.param("anon", "post", "/api/auth/register", {"name": "A", "email": email(20), "password": long(129)}, "password", id="register-password"),
    pytest.param("anon", "post", "/api/auth/login", {"email": email(255), "password": PASSWORD}, "email", id="login-email"),
    pytest.param("anon", "post", "/api/auth/login", {"email": email(20), "password": long(129)}, "password", id="login-password"),
    pytest.param("anon", "post", "/api/auth/refresh", {"refresh": long(1025)}, "refresh", id="refresh"),
    pytest.param("anon", "post", "/api/auth/logout", {"refresh": long(1025)}, "refresh", id="logout"),
    pytest.param("anon", "post", "/api/auth/verify-email", {"token": long(513)}, "token", id="verify-email"),
    pytest.param("anon", "post", "/api/auth/resend-verification", {"email": email(255)}, "email", id="resend-email"),
    pytest.param("anon", "post", "/api/auth/password-reset", {"email": email(255)}, "email", id="reset-email"),
    pytest.param("anon", "post", "/api/auth/password-reset/confirm", {"uid": long(65), "token": "t", "new_password": PASSWORD}, "uid", id="reset-uid"),
    pytest.param("anon", "post", "/api/auth/password-reset/confirm", {"uid": "MQ", "token": long(129), "new_password": PASSWORD}, "token", id="reset-token"),
    pytest.param("anon", "post", "/api/auth/password-reset/confirm", {"uid": "MQ", "token": "t", "new_password": long(129)}, "new_password", id="reset-password"),
    pytest.param("teacher", "post", "/api/rooms", {"name": long(101)}, "name", id="room-name"),
    pytest.param("teacher", "post", "/api/rooms", {"name": "R", "code": "ABCDEFGHJKM"}, "code", id="room-code"),
    pytest.param("teacher", "patch", lambda w: f"/api/rooms/{w.room.id}", {"name": long(101)}, "name", id="room-rename"),
    pytest.param("teacher", "post", "/api/quizzes", {**quiz_with(), "title": long(201)}, "title", id="quiz-title"),
    pytest.param("teacher", "post", "/api/quizzes", quiz_with(prompt=long(1001)), "questions.0.prompt", id="quiz-prompt"),
    pytest.param("teacher", "post", "/api/quizzes", quiz_with(explanation=long(2001)), "questions.0.explanation", id="quiz-explanation"),
    pytest.param("teacher", "post", "/api/quizzes", quiz_with(choices=["A", long(301)]), "questions.0.choices.1", id="quiz-choice"),
    pytest.param("teacher", "post", "/api/quizzes", quiz_with(choices=list("ABCDEFG")), "questions.0.choices", id="quiz-choice-count"),
    pytest.param("teacher", "post", "/api/quizzes", quiz_with(type="SA", choices=[], accepted_answers=[long(501)]), "questions.0.accepted_answers.0", id="quiz-accepted-answer"),
    pytest.param("teacher", "post", "/api/quizzes", {"title": "T", "questions": [{"type": "TF", "prompt": "Q?"}] * 101}, "questions", id="quiz-question-count"),
    pytest.param("teacher", "post", lambda w: f"/api/rooms/{w.room.id}/activities", quick(prompt=long(1001)), "question", id="quick-prompt"),
    pytest.param("teacher", "post", lambda w: f"/api/rooms/{w.room.id}/activities", quick(choices=["A", long(301)]), "question", id="quick-choice"),
    pytest.param("teacher", "post", lambda w: f"/api/rooms/{w.room.id}/activities", quick(choices=list("ABCDEFG")), "question", id="quick-choice-count"),
    pytest.param("teacher", "get", lambda w: f"/api/activities/{w.ended.id}/report.csv?tz={long(65)}", None, "tz", id="csv-tz"),
    pytest.param("anon", "post", join_url("PHYS5X"), {"name": long(41)}, "name", id="join-name"),
    pytest.param("student", "put", lambda w: response_url(w.sa.questions.get()), {"text_answer": long(501)}, "text_answer", id="answer-text"),
]


@pytest.mark.parametrize("client_kind, method, path, body, field", LENGTH_CASES)
def test_every_text_input_has_a_length_limit(world, teacher_client, client_kind, method, path, body, field):
    client = {"anon": APIClient(), "teacher": teacher_client, "student": APIClient()}[client_kind]
    if client_kind == "student":
        client.credentials(HTTP_X_PARTICIPANT_TOKEN=world.sa_token)
    before = snapshot()

    response = getattr(client, method)(path(world) if callable(path) else path, body, format="json")

    assert response.status_code == 400, response.content
    assert response.json()["code"] == "validation_error"
    assert field in response.json()["fields"], response.json()
    assert snapshot() == before


@pytest.mark.parametrize(
    "client_kind, path",
    [("anon", join_url("PHYS5X")), ("anon", "/api/auth/password-reset"), ("teacher", "/api/quizzes")],
)
def test_oversized_bodies_are_a_json_413(world, teacher_client, settings, client_kind, path):
    settings.DATA_UPLOAD_MAX_MEMORY_SIZE = 10_000
    client = teacher_client if client_kind == "teacher" else APIClient()
    body = json.dumps({"name": "x", "email": "a@example.com", "padding": long(20_000)})
    before = snapshot()

    response = client.generic("POST", path, body, content_type="application/json")

    assert response.status_code == 413
    assert response["Content-Type"] == "application/json"
    assert response.json() == {"detail": "Request body is too large.", "code": "request_too_large"}
    assert snapshot() == before


# --- No answer leaks to students --------------------------------------------

LEAK_MC = {
    "type": "MC",
    "prompt": "Capital of France?",
    "choices": ["Berlin", "Paris"],
    "correct_index": 1,
    "explanation": "SECRET-MC-EXPLANATION",
}
LEAK_TF = {"type": "TF", "prompt": "2 + 2 = 5", "correct_index": 1, "explanation": "SECRET-TF-EXPLANATION"}
LEAK_SA = {
    "type": "SA",
    "prompt": "Largest French city?",
    "accepted_answers": ["SECRET-ACCEPTED-ANSWER"],
    "explanation": "SECRET-SA-EXPLANATION",
}
# Grace never answers TF or SA, so these may never reach her.
NEVER_SHOWN = ["SECRET-TF-EXPLANATION", "SECRET-SA-EXPLANATION", "SECRET-ACCEPTED-ANSWER"]
# Another student's name and answer.
CLASSMATE_NAME, CLASSMATE_ANSWER = "Alan Turing", "alan's own answer"
# Student sockets get notifications only (PRD §9): never state.
STUDENT_EVENT_KEYS = {"type", "activity_id", "version", "participant_id"}


@pytest.mark.parametrize("show_feedback", [False, True], ids=["feedback-off", "feedback-on"])
@pytest.mark.parametrize("mode", [ActivityMode.TEACHER_PACED, ActivityMode.STUDENT_PACED])
def test_nothing_a_student_can_reach_leaks_answers(
    room, sent, django_capture_on_commit_callbacks, mode, show_feedback
):
    """Grace walks through a whole activity while a classmate answers everything right.
    Every body she can fetch and every event her socket gets is collected and checked."""
    with django_capture_on_commit_callbacks(execute=True):
        activity = launch(room, [LEAK_MC, LEAK_TF, LEAK_SA], mode=mode, show_feedback=show_feedback)
        mc, tf, sa = activity.questions.order_by("order")
        teacher_paced = mode == ActivityMode.TEACHER_PACED

        classmate = APIClient()
        classmate.credentials(HTTP_X_PARTICIPANT_TOKEN=joined("BIO3AB", CLASSMATE_NAME).token)
        for question, body in [(mc, {"choice_index": 1}), (tf, {"choice_index": 1}), (sa, {"text_answer": CLASSMATE_ANSWER})]:
            if teacher_paced:
                services.navigate(activity, question.order)
            assert classmate.put(response_url(question), body, format="json").status_code == 200
        if teacher_paced:
            services.navigate(activity, 0)

        grace = APIClient()
        seen = [grace.get("/api/rooms/BIO3AB/public")]
        joined_response = grace.post(join_url("BIO3AB"), {"name": "Grace"}, format="json")
        seen.append(joined_response)
        grace.credentials(HTTP_X_PARTICIPANT_TOKEN=joined_response.json()["token"])

        # Grace answers the MC question only, and looks at every question.
        for question in (mc, tf, sa):
            if teacher_paced:
                services.navigate(activity, question.order)
            seen.append(grace.get(STATE))
            if question == mc:
                seen.append(grace.put(response_url(mc), {"choice_index": 0}, format="json"))
        if not teacher_paced:
            seen.append(grace.post(FINISH))
        services.end_activity(activity)
        seen.append(grace.get(STATE))

    assert all(response.status_code in (200, 201) for response in seen)
    text = "".join(response.content.decode() for response in seen)
    for body in seen:
        assert keys_in(body.json()) & ANSWER_KEYS == set()
    for secret in [*NEVER_SHOWN, CLASSMATE_NAME, CLASSMATE_ANSWER]:
        assert secret not in text
    # Her own locked MC answer shows its feedback only when feedback is on (A2).
    assert ("SECRET-MC-EXPLANATION" in text) == show_feedback

    student_events = [event for group, event in payloads(sent) if group == "room.BIO3AB"]
    assert student_events
    for event in student_events:
        assert set(event) <= STUDENT_EVENT_KEYS, event
