from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from flashform.renderers import CSVRenderer
from flashform.serializers import ErrorSerializer
from rooms.services import owned_rooms

from . import services
from .models import ActivityType
from .serializers import (
    ActivityStartSerializer,
    ActivityUpdateSerializer,
    JoinResultSerializer,
    JoinSerializer,
    NavigateSerializer,
    ParticipantStateSerializer,
    ReportCSVQuerySerializer,
    ReportListQuerySerializer,
    ReportSerializer,
    ResponseSubmitSerializer,
    StudentResponseSerializer,
    TeacherStateSerializer,
)
from .throttles import RoomJoinRateThrottle

NOT_OWNED = "404 when the activity does not exist or belongs to another teacher."
ACTIVITY_ID = OpenApiParameter("id", int, OpenApiParameter.PATH, description="Activity id.")
PARTICIPANT_TOKEN = OpenApiParameter(
    "X-Participant-Token",
    str,
    OpenApiParameter.HEADER,
    required=True,
    description="The token returned by POST /rooms/{code}/join.",
)
BAD_TOKEN = (
    "401 `invalid_participant_token` when the token is missing, unknown, or the "
    "participant left or was removed."
)


# --- Teacher ----------------------------------------------------------------


class ActivityCreateView(APIView):
    @extend_schema(
        operation_id="activities_create",
        tags=["activities"],
        parameters=[OpenApiParameter("room_id", int, OpenApiParameter.PATH, description="Room id.")],
        request=ActivityStartSerializer,
        responses={
            201: TeacherStateSerializer,
            400: ErrorSerializer,
            401: ErrorSerializer,
            404: ErrorSerializer,
        },
        description="Start a Quick Question (`QUICK` + `question`) or a quiz (`QUIZ` + "
        "`quiz_id` + `mode`: `TEACHER_PACED` or `STUDENT_PACED`; `shuffle_questions` only with "
        "`STUDENT_PACED`). A quiz's questions are copied into "
        "the activity, so later edits to the quiz don't affect it. Any LIVE activity in the "
        "room is ended first, in the same transaction. 404 `not_found` when the room does not "
        "exist or belongs to another teacher; 404 `quiz_not_found` likewise for the quiz (the "
        "LIVE activity then keeps running).",
    )
    def post(self, request, room_id):
        room = get_object_or_404(owned_rooms(request.user), pk=room_id)
        serializer = ActivityStartSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if data["type"] == ActivityType.QUICK:
            activity = services.start_quick_activity(room, **data["question"])
        else:
            activity = services.start_quiz_activity(
                room,
                data["quiz_id"],
                mode=data["mode"],
                show_feedback=data["show_feedback"],
                shuffle_questions=data["shuffle_questions"],
            )
        state = services.get_teacher_state(activity)
        return Response(TeacherStateSerializer(state).data, status=status.HTTP_201_CREATED)


class TeacherStateView(APIView):
    @extend_schema(
        operation_id="activities_teacher_state",
        tags=["activities"],
        parameters=[ACTIVITY_ID],
        responses={200: TeacherStateSerializer, 401: ErrorSerializer, 404: ErrorSerializer},
        description="Full teacher view: activity, questions with answers, participants, "
        "responses and per-question summaries. " + NOT_OWNED,
    )
    def get(self, request, pk):
        activity = get_object_or_404(services.owned_activities(request.user), pk=pk)
        return Response(TeacherStateSerializer(services.get_teacher_state(activity)).data)


class ActivityNavigateView(APIView):
    @extend_schema(
        operation_id="activities_navigate",
        tags=["activities"],
        parameters=[ACTIVITY_ID],
        request=NavigateSerializer,
        responses={
            200: TeacherStateSerializer,
            400: ErrorSerializer,
            401: ErrorSerializer,
            404: ErrorSerializer,
            409: ErrorSerializer,
        },
        description="Teacher-paced: show question `index` to everyone (Next / Previous). "
        "Answers to the question being left are locked. Navigating to the current question "
        "is a no-op, so retries are safe. 400 `invalid_index` when out of range; 409 "
        "`activity_ended` or `not_teacher_paced`. " + NOT_OWNED,
    )
    def post(self, request, pk):
        activity = get_object_or_404(services.owned_activities(request.user), pk=pk)
        serializer = NavigateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        activity = services.navigate(activity, serializer.validated_data["index"])
        return Response(TeacherStateSerializer(services.get_teacher_state(activity)).data)


class ActivityEndView(APIView):
    @extend_schema(
        operation_id="activities_end",
        tags=["activities"],
        parameters=[ACTIVITY_ID],
        request=None,
        responses={200: TeacherStateSerializer, 401: ErrorSerializer, 404: ErrorSerializer},
        description="End the activity and lock all its responses. Ending an already ended "
        "activity is a no-op (200). " + NOT_OWNED,
    )
    def post(self, request, pk):
        activity = get_object_or_404(services.owned_activities(request.user), pk=pk)
        activity = services.end_activity(activity)
        return Response(TeacherStateSerializer(services.get_teacher_state(activity)).data)


class ActivityVoteView(APIView):
    @extend_schema(
        operation_id="activities_vote",
        tags=["activities"],
        parameters=[ACTIVITY_ID],
        request=None,
        responses={
            201: TeacherStateSerializer,
            401: ErrorSerializer,
            404: ErrorSerializer,
            409: ErrorSerializer,
        },
        description="Start Vote (PRD QQ4): end this LIVE short answer quick question and "
        "start a quick MC question with the same prompt whose options are its distinct "
        "answers (trimmed, case-insensitive; spelled as first submitted, in submission "
        "order). Returns the new activity. 409 `not_short_answer` unless this is an SA quick "
        "question; `activity_ended` when it is no longer LIVE (e.g. a retry after the vote "
        "started); `not_enough_answers` with fewer than 2 distinct answers. " + NOT_OWNED,
    )
    def post(self, request, pk):
        activity = get_object_or_404(services.owned_activities(request.user), pk=pk)
        vote = services.start_vote(activity)
        state = services.get_teacher_state(vote)
        return Response(TeacherStateSerializer(state).data, status=status.HTTP_201_CREATED)


class ParticipantRemoveView(APIView):
    @extend_schema(
        operation_id="activities_participant_remove",
        tags=["activities"],
        parameters=[
            ACTIVITY_ID,
            OpenApiParameter(
                "participant_id", OpenApiTypes.UUID, OpenApiParameter.PATH, description="Participant id."
            ),
        ],
        responses={204: None, 401: ErrorSerializer, 404: ErrorSerializer},
        description="Remove a participant (PRD L5): their token stops working, they leave the "
        "live view and the report, and their screen gets `participant_removed`. They can join "
        "again unless the room is locked. Works on ended activities too. Removing someone "
        "already removed (or who left) is a no-op (204), so retries are safe. 404 "
        "`participant_not_found` when they aren't in this activity. " + NOT_OWNED,
    )
    def delete(self, request, pk, participant_id):
        activity = get_object_or_404(services.owned_activities(request.user), pk=pk)
        services.remove_participant(activity, participant_id)
        return Response(status=status.HTTP_204_NO_CONTENT)


# --- Reports ----------------------------------------------------------------


class ReportListView(APIView):
    @extend_schema(
        operation_id="reports_list",
        tags=["reports"],
        parameters=[ReportListQuerySerializer],
        responses={
            200: ReportSerializer(many=True),
            400: ErrorSerializer,
            401: ErrorSerializer,
            404: ErrorSerializer,
        },
        description="Your ENDED activities, newest first, with participant count and average "
        "score (PRD RP1). `room` limits them to one room; 404 `not_found` when that room does "
        "not exist or belongs to another teacher. For a report's detail, use "
        "`GET /activities/{id}/teacher-state`.",
    )
    def get(self, request):
        query = ReportListQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        room = None
        if "room" in query.validated_data:
            room = get_object_or_404(owned_rooms(request.user), pk=query.validated_data["room"])
        reports = services.list_reports(request.user, room)
        return Response(ReportSerializer(reports, many=True).data)


class ActivityDetailView(APIView):
    @extend_schema(
        operation_id="activities_partial_update",
        tags=["activities"],
        parameters=[ACTIVITY_ID],
        request=ActivityUpdateSerializer,
        responses={
            200: TeacherStateSerializer,
            400: ErrorSerializer,
            401: ErrorSerializer,
            404: ErrorSerializer,
        },
        description="Change display settings: `hide_results` (PRD L4). Works on ended "
        "activities too. The teacher's socket gets `activity_updated`; students get nothing "
        "(they never see results). Setting the current value is a no-op, so retries are safe. "
        + NOT_OWNED,
    )
    def patch(self, request, pk):
        activity = get_object_or_404(services.owned_activities(request.user), pk=pk)
        serializer = ActivityUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        activity = services.update_activity(activity, **serializer.validated_data)
        return Response(TeacherStateSerializer(services.get_teacher_state(activity)).data)

    @extend_schema(
        operation_id="activities_destroy",
        tags=["reports"],
        parameters=[ACTIVITY_ID],
        responses={204: None, 401: ErrorSerializer, 404: ErrorSerializer, 409: ErrorSerializer},
        description="Delete an ended activity's report: its questions, participants and "
        "answers (PRD RP4). 409 `activity_live` while it is running: end it first. " + NOT_OWNED,
    )
    def delete(self, request, pk):
        activity = get_object_or_404(services.owned_activities(request.user), pk=pk)
        services.delete_report(activity)
        return Response(status=status.HTTP_204_NO_CONTENT)


class ReportCSVView(APIView):
    # Always CSV, whatever the Accept header says (fetch wrappers often send
    # application/json); errors are still JSON, see handle_exception.
    renderer_classes = [CSVRenderer]

    def perform_content_negotiation(self, request, force=False):
        return super().perform_content_negotiation(request, force=True)

    def handle_exception(self, exc):
        self.request.accepted_renderer = JSONRenderer()
        self.request.accepted_media_type = JSONRenderer.media_type
        return super().handle_exception(exc)

    @extend_schema(
        operation_id="reports_csv",
        tags=["reports"],
        parameters=[ACTIVITY_ID, ReportCSVQuerySerializer],
        responses={
            (200, "text/csv"): OpenApiTypes.STR,
            (400, "application/json"): ErrorSerializer,
            (401, "application/json"): ErrorSerializer,
            (404, "application/json"): ErrorSerializer,
        },
        description="The report as a CSV download, UTF-8 with a byte order mark (PRD RP3): "
        "one row per participant with `name, joined_at, finished_at, score, total_possible, "
        "percent`, then one column per question (header `Q1: <prompt>`, prompt truncated to "
        "60 characters) holding the answer text. Works for LIVE activities too (a snapshot). "
        "Errors are JSON. " + NOT_OWNED,
    )
    def get(self, request, pk):
        activity = get_object_or_404(
            services.owned_activities(request.user).select_related("room"), pk=pk
        )
        query = ReportCSVQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        content = services.report_csv(activity, query.validated_data["tz"])
        filename = services.report_filename(activity)
        return Response(content, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# --- Student ----------------------------------------------------------------


class JoinView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle, RoomJoinRateThrottle]
    throttle_scope = "join_ip"

    @extend_schema(
        operation_id="rooms_join",
        tags=["student"],
        parameters=[
            OpenApiParameter(
                "code",
                str,
                OpenApiParameter.PATH,
                description="Room code as typed; case-insensitive, spaces ignored.",
            ),
            OpenApiParameter(
                "X-Participant-Token",
                str,
                OpenApiParameter.HEADER,
                required=False,
                description="Optional: your token from an earlier (or the current) activity in "
                "this room. Lets you re-join while the room is locked.",
            ),
        ],
        request=JoinSerializer,
        responses={
            201: JoinResultSerializer,
            400: ErrorSerializer,
            404: ErrorSerializer,
            409: ErrorSerializer,
            423: ErrorSerializer,
            429: ErrorSerializer,
        },
        description="Join the room's LIVE activity. 404 `room_not_found`, 423 `room_locked` "
        "(unless `X-Participant-Token` shows you already joined this room and weren't removed), "
        "409 `no_live_activity` (wait for the teacher), 429 `throttled`. The token is only "
        "returned here.",
    )
    def post(self, request, code):
        serializer = JoinSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = services.join_room(
            code,
            serializer.validated_data["name"],
            token=request.headers.get("X-Participant-Token"),
        )
        return Response(JoinResultSerializer(result).data, status=status.HTTP_201_CREATED)


class ParticipantView(APIView):
    """Base for endpoints authenticated by `X-Participant-Token` instead of a JWT."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def get_participant(self, request):
        return services.authenticate_participant(request.headers.get("X-Participant-Token"))


class ParticipantStateView(ParticipantView):
    @extend_schema(
        operation_id="participant_state",
        tags=["student"],
        parameters=[PARTICIPANT_TOKEN],
        responses={200: ParticipantStateSerializer, 401: ErrorSerializer},
        description="Everything the student screen needs. Correct answers appear only in "
        "`response.feedback`, and only when feedback is on and that answer is locked. "
        + BAD_TOKEN,
    )
    def get(self, request):
        state = services.get_participant_state(self.get_participant(request))
        return Response(ParticipantStateSerializer(state).data)


class ParticipantResponseView(ParticipantView):
    @extend_schema(
        operation_id="participant_response_submit",
        tags=["student"],
        parameters=[
            PARTICIPANT_TOKEN,
            OpenApiParameter("question_id", int, OpenApiParameter.PATH, description="Question id."),
        ],
        request=ResponseSubmitSerializer,
        responses={
            200: StudentResponseSerializer,
            400: ErrorSerializer,
            401: ErrorSerializer,
            404: ErrorSerializer,
            409: ErrorSerializer,
        },
        description="Save your answer (upsert; safe to retry). 400 `invalid_answer`; 404 "
        "`question_not_found` if the question isn't in your activity; 409 `activity_ended`, "
        "`not_current_question`, `already_finished` or `response_locked`. " + BAD_TOKEN,
    )
    def put(self, request, question_id):
        participant = self.get_participant(request)
        serializer = ResponseSubmitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = services.submit_response(participant, question_id, **serializer.validated_data)
        return Response(StudentResponseSerializer(result).data)


class ParticipantFinishView(ParticipantView):
    @extend_schema(
        operation_id="participant_finish",
        tags=["student"],
        parameters=[PARTICIPANT_TOKEN],
        request=None,
        responses={200: ParticipantStateSerializer, 401: ErrorSerializer, 409: ErrorSerializer},
        description="Student-paced: finish the activity. Every answer given is locked (with "
        "feedback on, each now carries its `feedback`) and no more answers are accepted. "
        "Finishing again is a no-op (200), so retries are safe. 409 `activity_ended` or "
        "`not_student_paced`. " + BAD_TOKEN,
    )
    def post(self, request):
        state = services.finish(self.get_participant(request))
        return Response(ParticipantStateSerializer(state).data)


class ParticipantLeaveView(ParticipantView):
    @extend_schema(
        operation_id="participant_leave",
        tags=["student"],
        parameters=[PARTICIPANT_TOKEN],
        request=None,
        responses={204: None, 401: ErrorSerializer},
        description="Leave the activity: the token stops working; saved answers are kept. "
        + BAD_TOKEN,
    )
    def post(self, request):
        services.leave(self.get_participant(request))
        return Response(status=status.HTTP_204_NO_CONTENT)
