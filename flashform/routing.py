"""WebSocket URL routes (PRD §9). Consumers live in activities/consumers.py."""

from django.urls import path

from activities import consumers

websocket_urlpatterns = [
    path("ws/room/<str:code>/", consumers.RoomConsumer.as_asgi()),
    path("ws/teacher/room/<int:room_id>/", consumers.TeacherRoomConsumer.as_asgi()),
]
