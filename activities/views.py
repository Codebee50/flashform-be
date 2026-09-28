from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from flashform.serializers import ErrorSerializer
from rooms.services import owned_rooms

from . import services
from .models import ActivityType
from .serializers import (
    ActivityStartSerializer,
    JoinResultSerializer,
    JoinSerializer,
    NavigateSerializer,
    ParticipantStateSerializer,
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
        "`quiz_id` + `mode`; only `TEACHER_PACED` for now). A quiz's questions are copied into "
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
            )
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
        description="Join the room's LIVE activity. 404 `room_not_found`, 423 `room_locked`, "
        "409 `no_live_activity` (wait for the teacher), 429 `throttled`. The token is only "
        "returned here.",
    )
    def post(self, request, code):
        serializer = JoinSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = services.join_room(code, serializer.validated_data["name"])
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
