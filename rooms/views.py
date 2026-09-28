from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import status, viewsets
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from flashform.serializers import ErrorSerializer

from . import services
from .serializers import (
    PublicRoomSerializer,
    RoomCreateSerializer,
    RoomSerializer,
    RoomUpdateSerializer,
)

NOT_OWNED = "404 when the room does not exist or belongs to another teacher."
ROOM_ID = OpenApiParameter("id", int, OpenApiParameter.PATH, description="Room id.")


@extend_schema_view(
    list=extend_schema(
        operation_id="rooms_list",
        responses={200: RoomSerializer(many=True), 401: ErrorSerializer},
        description="The teacher's rooms, newest first.",
    ),
    create=extend_schema(
        operation_id="rooms_create",
        request=RoomCreateSerializer,
        responses={201: RoomSerializer, 400: ErrorSerializer, 401: ErrorSerializer},
        description="Without `code`, a unique 6-character code is generated. A custom code "
        "that is malformed or taken answers 400 `validation_error` with `fields.code`.",
    ),
    retrieve=extend_schema(
        operation_id="rooms_retrieve",
        parameters=[ROOM_ID],
        responses={200: RoomSerializer, 401: ErrorSerializer, 404: ErrorSerializer},
        description=NOT_OWNED,
    ),
    partial_update=extend_schema(
        operation_id="rooms_partial_update",
        parameters=[ROOM_ID],
        request=RoomUpdateSerializer,
        responses={
            200: RoomSerializer,
            400: ErrorSerializer,
            401: ErrorSerializer,
            404: ErrorSerializer,
        },
        description="Rename and/or lock or unlock. Only `name` and `is_locked` can change. "
        + NOT_OWNED,
    ),
    destroy=extend_schema(
        operation_id="rooms_destroy",
        parameters=[ROOM_ID],
        responses={204: None, 401: ErrorSerializer, 404: ErrorSerializer},
        description="Deletes the room with all its activities and responses. " + NOT_OWNED,
    ),
)
@extend_schema(tags=["rooms"])
class RoomViewSet(viewsets.GenericViewSet):
    serializer_class = RoomSerializer
    lookup_value_regex = r"\d+"

    def get_queryset(self):
        return services.owned_rooms(self.request.user)

    def list(self, request):
        return Response(RoomSerializer(self.get_queryset(), many=True).data)

    def create(self, request):
        serializer = RoomCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        room = services.create_room(owner=request.user, **serializer.validated_data)
        return Response(RoomSerializer(room).data, status=status.HTTP_201_CREATED)

    def retrieve(self, request, pk=None):
        return Response(RoomSerializer(self.get_object()).data)

    def partial_update(self, request, pk=None):
        room = self.get_object()
        serializer = RoomUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        room = services.update_room(room, **serializer.validated_data)
        return Response(RoomSerializer(room).data)

    def destroy(self, request, pk=None):
        services.delete_room(self.get_object())
        return Response(status=status.HTTP_204_NO_CONTENT)


class PublicRoomView(APIView):
    """Student lookup by code, before joining. No teacher auth."""

    authentication_classes = []
    permission_classes = [AllowAny]

    @extend_schema(
        operation_id="rooms_public",
        tags=["student"],
        parameters=[
            OpenApiParameter(
                "code",
                str,
                OpenApiParameter.PATH,
                description="Room code as typed; case-insensitive, spaces ignored.",
            )
        ],
        responses={200: PublicRoomSerializer, 404: ErrorSerializer},
        description="404 `room_not_found` for an unknown code. A locked room still answers "
        "200 with `is_locked: true`.",
    )
    def get(self, request, code):
        return Response(PublicRoomSerializer(services.get_public_room(code)).data)
