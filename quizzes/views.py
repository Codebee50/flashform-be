from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from flashform.serializers import ErrorSerializer

from . import services
from .serializers import QuizListItemSerializer, QuizSerializer, QuizWriteSerializer

NOT_OWNED = "404 when the quiz does not exist or belongs to another teacher."
QUIZ_ID = OpenApiParameter("id", int, OpenApiParameter.PATH, description="Quiz id.")
INVALID = (
    "400 `validation_error` when any rule of PRD Q1–Q5 is broken; `fields` keys are paths "
    "such as `title`, `questions` or `questions.2.choices` (0-based question index)."
)


@extend_schema_view(
    list=extend_schema(
        operation_id="quizzes_list",
        responses={200: QuizListItemSerializer(many=True), 401: ErrorSerializer},
        description="The teacher's quizzes, most recently edited first.",
    ),
    create=extend_schema(
        operation_id="quizzes_create",
        request=QuizWriteSerializer,
        responses={201: QuizSerializer, 400: ErrorSerializer, 401: ErrorSerializer},
        description="Create a quiz with its full list of questions. " + INVALID,
    ),
    retrieve=extend_schema(
        operation_id="quizzes_retrieve",
        parameters=[QUIZ_ID],
        responses={200: QuizSerializer, 401: ErrorSerializer, 404: ErrorSerializer},
        description="The quiz with its questions in order. " + NOT_OWNED,
    ),
    update=extend_schema(
        operation_id="quizzes_update",
        parameters=[QUIZ_ID],
        request=QuizWriteSerializer,
        responses={
            200: QuizSerializer,
            400: ErrorSerializer,
            401: ErrorSerializer,
            404: ErrorSerializer,
        },
        description="Replace the title and the whole questions array in one transaction. "
        "Questions missing from the body are deleted. Nothing changes on error. "
        + INVALID
        + " "
        + NOT_OWNED,
    ),
    destroy=extend_schema(
        operation_id="quizzes_destroy",
        parameters=[QUIZ_ID],
        responses={204: None, 401: ErrorSerializer, 404: ErrorSerializer},
        description="Deletes the quiz. Reports of activities run from it are kept. " + NOT_OWNED,
    ),
    duplicate=extend_schema(
        operation_id="quizzes_duplicate",
        parameters=[QUIZ_ID],
        request=None,
        responses={201: QuizSerializer, 401: ErrorSerializer, 404: ErrorSerializer},
        description='Copy the quiz and its questions as a new quiz titled "<title> (copy)". '
        + NOT_OWNED,
    ),
)
@extend_schema(tags=["quizzes"])
class QuizViewSet(viewsets.GenericViewSet):
    serializer_class = QuizSerializer
    lookup_value_regex = r"\d+"

    def get_queryset(self):
        quizzes = services.owned_quizzes(self.request.user)
        if self.action == "retrieve":
            quizzes = quizzes.prefetch_related("questions")
        return quizzes

    def list(self, request):
        return Response(QuizListItemSerializer(self.get_queryset(), many=True).data)

    def create(self, request):
        serializer = QuizWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        quiz = services.create_quiz(owner=request.user, **serializer.validated_data)
        return Response(QuizSerializer(quiz).data, status=status.HTTP_201_CREATED)

    def retrieve(self, request, pk=None):
        return Response(QuizSerializer(self.get_object()).data)

    def update(self, request, pk=None):
        quiz = self.get_object()
        serializer = QuizWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        quiz = services.replace_quiz(quiz, **serializer.validated_data)
        return Response(QuizSerializer(quiz).data)

    def destroy(self, request, pk=None):
        services.delete_quiz(self.get_object())
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"])
    def duplicate(self, request, pk=None):
        quiz = services.duplicate_quiz(self.get_object())
        return Response(QuizSerializer(quiz).data, status=status.HTTP_201_CREATED)
