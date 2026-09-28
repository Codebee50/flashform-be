"""WebSocket consumers (PRD §9). Sockets are server → client notifications only: the only
message a client may send is `{"type": "ping"}`; anything else is ignored.

A close code sent before `accept()` reaches browsers as a bare handshake failure, so on
rejection we accept and then close with an application code:

- 4401: the token is missing, invalid or expired (teacher: refresh and reconnect;
  student: drop the token and reconnect without it).
- 4404: the room doesn't exist, or the teacher doesn't own it (stop reconnecting).
"""

import json

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from . import broadcast, services
from .ws_auth import query_param

CLOSE_INVALID_TOKEN = 4401
CLOSE_NOT_FOUND = 4404


class NotificationConsumer(AsyncJsonWebsocketConsumer):
    group: str | None = None

    async def reject(self, code: int) -> None:
        await self.accept()
        await self.close(code=code)

    async def join_group(self, group: str) -> None:
        self.group = group
        await self.channel_layer.group_add(group, self.channel_name)
        await self.accept()

    async def disconnect(self, code):
        if self.group:
            await self.channel_layer.group_discard(self.group, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None, **kwargs):
        # Not super().receive: invalid JSON would raise and drop the socket.
        try:
            message = json.loads(text_data or "")
        except ValueError:
            return
        if isinstance(message, dict) and message.get("type") == "ping":
            await self.on_ping()
            await self.send_json({"type": "pong"})

    async def on_ping(self) -> None:
        pass

    async def broadcast_event(self, message):
        """Group message from `activities.broadcast.send`."""
        await self.send_json(message["event"])


class RoomConsumer(NotificationConsumer):
    """`ws/room/{code}/?token=`: students in a room. The token is optional (waiting screen)."""

    participant_id = None

    async def connect(self):
        room = await database_sync_to_async(services.get_room_by_code)(
            self.scope["url_route"]["kwargs"]["code"]
        )
        if room is None:
            return await self.reject(CLOSE_NOT_FOUND)

        token = query_param(self.scope, "token")
        if token:
            participant = await database_sync_to_async(services.participant_in_room)(token, room)
            if participant is None:
                return await self.reject(CLOSE_INVALID_TOKEN)
            self.participant_id = participant.id
            await database_sync_to_async(services.touch_participant)(participant.id)

        await self.join_group(broadcast.student_group(room.code))

    async def on_ping(self):
        if self.participant_id:
            await database_sync_to_async(services.touch_participant)(self.participant_id)


class TeacherRoomConsumer(NotificationConsumer):
    """`ws/teacher/room/{room_id}/?auth=<access token>`: the teacher's live view."""

    async def connect(self):
        user = self.scope.get("user")
        if user is None or not user.is_authenticated:
            return await self.reject(CLOSE_INVALID_TOKEN)

        room = await database_sync_to_async(services.get_owned_room)(
            user, self.scope["url_route"]["kwargs"]["room_id"]
        )
        if room is None:
            return await self.reject(CLOSE_NOT_FOUND)

        await self.join_group(broadcast.teacher_group(room.id))
