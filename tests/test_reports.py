"""Reports: list with participant counts and average scores, detail via teacher-state, CSV
export and deleting ended activities (PRD §5.8 RP1–RP4, §17 scoring)."""

import csv
import io
from datetime import UTC, datetime

import pytest

from activities import services
from activities.models import Activity, ActivityQuestion, Participant, Response
from rooms.models import Room
from tests.test_activities import (  # noqa: F401 (fixtures)
    LEAVE,
    MC_QUESTION,
    SA_QUESTION,
    TF_QUESTION,
    end_url,
    join,
    launch,
    other_client,
    other_teacher,
    question,
    response_url,
    room,
    teacher_state_url,
)

pytestmark = pytest.mark.django_db

REPORTS = "/api/activities"
FINISH = "/api/participant/finish"
# No correct answer: excluded from total_possible (PRD §17).
UNGRADED_MC = {"type": "MC", "prompt": "Favourite colour?", "choices": ["Red", "Blue"]}
QUESTIONS = [MC_QUESTION, TF_QUESTION, SA_QUESTION, UNGRADED_MC]


def csv_url(activity) -> str:
    return f"/api/activities/{activity.id}/report.csv"


def detail_url(activity) -> str:
    return f"/api/activities/{activity.id}"


def read_csv(response) -> list[list[str]]:
    body = response.content.decode("utf-8")
    assert body.startswith("﻿")
    return list(csv.reader(io.StringIO(body[1:])))


def answer(student, activity, order, **body):
    response = student.put(response_url(question(activity, order)), body, format="json")
    assert response.status_code == 200, response.json()


@pytest.fixture
def quiz(room):
    """An ENDED student-paced quiz: Zoë got all 3 graded questions right and finished,
    Grace got 1 right, Ben answered nothing."""
    activity = launch(room, QUESTIONS)
    zoe = join(name="Zoë")
    answer(zoe, activity, 0, choice_index=1)
    answer(zoe, activity, 1, choice_index=1)
    answer(zoe, activity, 2, text_answer="  paris ")
    answer(zoe, activity, 3, choice_index=0)
    assert zoe.post(FINISH).status_code == 200
    grace = join(name="Grace")
    answer(grace, activity, 0, choice_index=0)
    answer(grace, activity, 1, choice_index=1)
    answer(grace, activity, 3, choice_index=1)
    join(name="Ben")
    services.end_activity(activity)
    return activity


# --- Scoring ------------------------------------------------------------------


def test_teacher_state_scores_exclude_questions_without_a_correct_answer(teacher_client, quiz):
    state = teacher_client.get(teacher_state_url(quiz)).json()

    assert state["activity"]["status"] == "ENDED"
    assert state["total_possible"] == 3
    assert {p["name"]: p["score"] for p in state["participants"]} == {"Zoë": 3, "Grace": 1, "Ben": 0}


def test_csv_score_total_possible_and_percent(teacher_client, quiz):
    rows = read_csv(teacher_client.get(csv_url(quiz)))

    scores = {row[0]: row[3:6] for row in rows[1:]}
    assert scores == {
        "Zoë": ["3", "3", "100.0"],
        "Grace": ["1", "3", "33.3"],
        "Ben": ["0", "3", "0.0"],
    }


def test_quick_question_has_nothing_to_score(teacher_client, room):
    activity = services.start_quick_activity(room, type="MC")
    student = join(name="Grace")
    answer(student, activity, 0, choice_index=2)
    services.end_activity(activity)

    rows = read_csv(teacher_client.get(csv_url(activity)))
    [report] = teacher_client.get(REPORTS).json()

    assert rows[1][3:] == ["0", "0", "", "C"]
    assert report["total_possible"] == 0
    assert report["avg_score"] is None
    assert report["avg_percent"] is None


# --- CSV ----------------------------------------------------------------------


def test_csv_header_and_row_shape(teacher_client, quiz):
    response = teacher_client.get(csv_url(quiz))

    assert response.status_code == 200
    assert response["Content-Type"] == "text/csv; charset=utf-8"
    assert response["Content-Disposition"] == (
        f'attachment; filename="report-BIO3AB-{quiz.started_at:%Y-%m-%d}-{quiz.id}.csv"'
    )
    rows = read_csv(response)
    assert rows[0] == [
        "name",
        "joined_at",
        "finished_at",
        "score",
        "total_possible",
        "percent",
        "Q1: Capital of France?",
        "Q2: The sky is green.",
        "Q3: Largest French city?",
        "Q4: Favourite colour?",
    ]
    # One row per participant, in join order, every row as wide as the header.
    assert [row[0] for row in rows[1:]] == ["Zoë", "Grace", "Ben"]
    assert {len(row) for row in rows} == {10}
    # Answer text: the chosen option (MC/TF), the typed answer (SA), blank if unanswered.
    assert rows[1][6:] == ["Paris", "False", "paris", "Red"]
    assert rows[2][6:] == ["Berlin", "False", "", "Blue"]
    assert rows[3][6:] == ["", "", "", ""]
    # Only Zoë pressed Finish.
    assert rows[1][2] != "" and rows[2][2] == "" and rows[3][2] == ""


def test_csv_is_utf8_with_bom_so_excel_reads_accents(teacher_client, quiz):
    content = teacher_client.get(csv_url(quiz)).content

    assert content.startswith(b"\xef\xbb\xbf")
    assert "Zoë".encode() in content


def test_csv_header_prompts_are_numbered_and_truncated_to_60_chars(teacher_client, room):
    long_prompt = "Which of these\nnumbers is prime? " + "x" * 80
    activity = launch(room, [{**UNGRADED_MC, "prompt": long_prompt}, {**UNGRADED_MC, "prompt": ""}])
    services.end_activity(activity)

    header = read_csv(teacher_client.get(csv_url(activity)))[0]

    prompt = header[6].removeprefix("Q1: ")
    assert len(prompt) == 60
    assert prompt.startswith("Which of these numbers is prime? xxx")
    assert prompt.endswith("…")
    assert header[7] == "Q2"


def test_csv_numbers_duplicate_names_and_defuses_formulas(teacher_client, room):
    activity = launch(room, [{"type": "SA", "prompt": "Say something"}])
    for name in ["Grace", "=HYPERLINK(1)", "Grace"]:
        student = join(name=name)
        answer(student, activity, 0, text_answer="-2+3")
    services.end_activity(activity)

    rows = read_csv(teacher_client.get(csv_url(activity)))

    assert [row[0] for row in rows[1:]] == ["Grace", "'=HYPERLINK(1)", "Grace (2)"]
    assert {row[6] for row in rows[1:]} == {"'-2+3"}


def test_csv_times_in_the_requested_time_zone(teacher_client, quiz):
    Participant.objects.update(created_at=datetime(2026, 9, 28, 8, 5, 9, tzinfo=UTC))

    utc = read_csv(teacher_client.get(csv_url(quiz)))
    lagos = read_csv(teacher_client.get(csv_url(quiz), {"tz": "Africa/Lagos"}))

    assert utc[1][1] == "2026-09-28 08:05:09"
    assert lagos[1][1] == "2026-09-28 09:05:09"


@pytest.mark.parametrize(
    "tz",
    [
        "Mars/Olympus",
        "America",  # a tzdata directory: ZoneInfo raises IsADirectoryError
        "Europe/" + "x" * 300,  # over max_length; ZoneInfo alone would raise OSError
    ],
)
def test_csv_unknown_time_zone_is_a_json_400(teacher_client, quiz, tz):
    response = teacher_client.get(csv_url(quiz), {"tz": tz})

    assert response.status_code == 400
    assert response["Content-Type"] == "application/json"
    assert response.json()["code"] == "validation_error"
    assert "tz" in response.json()["fields"]


def test_csv_ignores_accept_header_and_errors_stay_json(teacher_client, other_client, quiz):
    ok = teacher_client.get(csv_url(quiz), HTTP_ACCEPT="application/json")
    missing = other_client.get(csv_url(quiz), HTTP_ACCEPT="text/csv")

    assert ok.status_code == 200
    assert ok["Content-Type"] == "text/csv; charset=utf-8"
    assert missing.status_code == 404
    assert missing["Content-Type"] == "application/json"
    assert missing.json()["code"] == "not_found"


def test_csv_needs_a_teacher(api_client, quiz):
    response = api_client.get(csv_url(quiz))

    assert response.status_code == 401
    assert response.json()["code"] == "not_authenticated"


# --- List -----------------------------------------------------------------------


def test_list_reports_ended_activities_newest_first(teacher_client, teacher, room, quiz):
    other_room = Room.objects.create(owner=teacher, name="Period 5 Chemistry", code="CHEM5X")
    quick = services.start_quick_activity(other_room, type="TF")
    services.end_activity(quick)
    live = services.start_quick_activity(room, type="MC")

    reports = teacher_client.get(REPORTS).json()

    assert [report["id"] for report in reports] == [quick.id, quiz.id]
    assert live.id not in [report["id"] for report in reports]
    assert reports[1] == {
        "id": quiz.id,
        "room": {"id": room.id, "name": "Period 3 Biology", "code": "BIO3AB"},
        "type": "QUIZ",
        "mode": "STUDENT_PACED",
        "quiz_title": "",
        "started_at": reports[1]["started_at"],
        "ended_at": reports[1]["ended_at"],
        "participant_count": 3,
        "question_count": 4,
        "total_possible": 3,
        "avg_score": 1.33,  # (3 + 1 + 0) / 3
        "avg_percent": 44.4,
    }
    assert reports[1]["ended_at"] is not None
    assert reports[0]["participant_count"] == 0
    assert reports[0]["avg_score"] is None


def test_list_filtered_by_room(teacher_client, teacher, quiz):
    other_room = Room.objects.create(owner=teacher, name="Chemistry", code="CHEM5X")
    services.end_activity(services.start_quick_activity(other_room, type="TF"))

    reports = teacher_client.get(REPORTS, {"room": other_room.id}).json()

    assert [report["room"]["code"] for report in reports] == ["CHEM5X"]


def test_list_with_an_invalid_room_is_a_400(teacher_client):
    response = teacher_client.get(REPORTS, {"room": "abc"})

    assert response.status_code == 400
    assert "room" in response.json()["fields"]


def test_list_query_count_does_not_grow_with_activities(
    teacher_client, room, django_assert_max_num_queries
):
    for _ in range(5):
        activity = launch(room, QUESTIONS)
        answer(join(name="Grace"), activity, 0, choice_index=1)
        services.end_activity(activity)

    # 1 user lookup for the JWT + activities + participants + responses + questions.
    with django_assert_max_num_queries(5):
        assert len(teacher_client.get(REPORTS).json()) == 5


# --- Leaving ---------------------------------------------------------------------


def test_leaving_after_the_end_keeps_the_student_in_the_report(teacher_client, room):
    activity = launch(room, QUESTIONS)
    student = join(name="Grace")
    answer(student, activity, 0, choice_index=1)
    services.end_activity(activity)
    version = Activity.objects.get(pk=activity.pk).version

    assert student.post(LEAVE).status_code == 204

    assert not Participant.objects.get().is_removed
    assert Activity.objects.get(pk=activity.pk).version == version
    assert teacher_client.get(REPORTS).json()[0]["participant_count"] == 1
    assert [row[0] for row in read_csv(teacher_client.get(csv_url(activity)))[1:]] == ["Grace"]


def test_leaving_while_live_drops_the_student_from_the_report(teacher_client, room):
    activity = launch(room, QUESTIONS)
    leaver = join(name="Grace")
    answer(leaver, activity, 0, choice_index=1)
    assert leaver.post(LEAVE).status_code == 204
    join(name="Ben")
    services.end_activity(activity)

    [report] = teacher_client.get(REPORTS).json()
    rows = read_csv(teacher_client.get(csv_url(activity)))

    assert report["participant_count"] == 1
    assert report["avg_score"] == 0
    assert [row[0] for row in rows[1:]] == ["Ben"]


# --- Delete ----------------------------------------------------------------------


def test_delete_an_ended_report(teacher_client, quiz):
    response = teacher_client.delete(detail_url(quiz))

    assert response.status_code == 204
    assert not Activity.objects.exists()
    assert not ActivityQuestion.objects.exists()
    assert not Participant.objects.exists()
    assert not Response.objects.exists()
    assert teacher_client.get(REPORTS).json() == []


def test_cannot_delete_a_live_activity(teacher_client, room):
    activity = launch(room, QUESTIONS)
    join(name="Grace")

    response = teacher_client.delete(detail_url(activity))

    assert response.status_code == 409
    assert response.json()["code"] == "activity_live"
    assert Activity.objects.get(pk=activity.pk).is_live
    assert Participant.objects.count() == 1


def test_delete_then_end_first(teacher_client, room):
    activity = launch(room, QUESTIONS)

    assert teacher_client.post(end_url(activity)).status_code == 200
    assert teacher_client.delete(detail_url(activity)).status_code == 204


# --- Ownership -------------------------------------------------------------------


def test_other_teachers_reports_are_invisible(other_client, room, quiz):
    assert other_client.get(REPORTS).json() == []
    missing_room = other_client.get(REPORTS, {"room": room.id})
    assert missing_room.status_code == 404
    assert missing_room.json()["code"] == "not_found"
    assert other_client.get(csv_url(quiz)).status_code == 404
    assert other_client.get(teacher_state_url(quiz)).status_code == 404


def test_other_teacher_cannot_delete_a_report(other_client, quiz):
    response = other_client.delete(detail_url(quiz))

    assert response.status_code == 404
    assert Activity.objects.filter(pk=quiz.pk).exists()


def test_reports_need_a_teacher(api_client, quiz):
    assert api_client.get(REPORTS).status_code == 401
    assert api_client.delete(detail_url(quiz)).status_code == 401
    assert Activity.objects.exists()
