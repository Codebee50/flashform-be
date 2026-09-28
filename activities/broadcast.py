"""Server → client WebSocket notifications (PRD §6 rules 1, 3, 6; §9).

Events carry no state, only `{"type", "activity_id", "version"}` (plus `participant_id`
for `participant_removed`): clients refetch state over REST. Services call these helpers
inside their transaction; the send itself is deferred with `transaction.on_commit`, so
clients never refetch before the change is visible. A failed send (Redis or broker down)
is logged and swallowed: the write already committed, and clients also poll.

Groups:
- `room.{code}`: every student socket in the room, including the waiting screen.
- `teacher.room.{room_id}`: the teacher's live view.

`responses_updated` and `participants_changed` are throttled to one event per
THROTTLE_MS per activity, with a guaranteed trailing event (see `send_throttled`).
"""

import logging
import threading
import time

import redis
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.db import transaction

logger = logging.getLogger(__name__)

THROTTLE_MS = 500
# How long a "trailing event scheduled" flag lives. The trailing task deletes it after
# THROTTLE_MS; the TTL only matters if the worker is down, so throttling recovers.
PENDING_MS = 5000

ACTIVITY_STARTED = "activity_started"
ACTIVITY_UPDATED = "activity_updated"
ACTIVITY_ENDED = "activity_ended"
PARTICIPANT_REMOVED = "participant_removed"
PARTICIPANTS_CHANGED = "participants_changed"
RESPONSES_UPDATED = "responses_updated"
THROTTLED_EVENTS = {PARTICIPANTS_CHANGED, RESPONSES_UPDATED}


def student_group(room_code: str) -> str:
    return f"room.{room_code}"


def teacher_group(room_id: int) -> str:
    return f"teacher.room.{room_id}"


def event(type: str, activity_id: int, version: int, **extra) -> dict:
    return {"type": type, "activity_id": activity_id, "version": version, **extra}


# --- Sending ----------------------------------------------------------------


def send(group: str, payload: dict) -> None:
    """Deliver one event to a group now. Never raises."""
    try:
        async_to_sync(get_channel_layer().group_send)(
            group, {"type": "broadcast.event", "event": payload}
        )
    except Exception:
        logger.exception("Could not broadcast %s to %s", payload.get("type"), group)


def _after_commit(group: str, payload: dict) -> None:
    transaction.on_commit(lambda: send(group, payload))


# --- Throttling -------------------------------------------------------------


class MemoryStore:
    """In-process SET NX PX / DEL, for tests and single-process runs without Redis."""

    def __init__(self):
        self._expiry: dict[str, float] = {}
        self._lock = threading.Lock()

    def set(self, key: str, *, px: int, nx: bool = False) -> bool:
        now = time.monotonic()
        with self._lock:
            if nx and self._expiry.get(key, 0) > now:
                return False
            self._expiry[key] = now + px / 1000
            return True

    def delete(self, key: str) -> None:
        with self._lock:
            self._expiry.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._expiry.clear()


class RedisStore:
    def __init__(self, url: str):
        self._redis = redis.Redis.from_url(url)

    def set(self, key: str, *, px: int, nx: bool = False) -> bool:
        return bool(self._redis.set(key, 1, px=px, nx=nx))

    def delete(self, key: str) -> None:
        self._redis.delete(key)


_store = None
_store_lock = threading.Lock()


def get_store():
    """Redis when BROADCAST_REDIS_URL is set, else an in-process store."""
    global _store
    with _store_lock:
        if _store is None:
            url = getattr(settings, "BROADCAST_REDIS_URL", "")
            _store = RedisStore(url) if url else MemoryStore()
        return _store


def _lock_key(type: str, activity_id: int) -> str:
    return f"broadcast:lock:{type}:{activity_id}"


def _pending_key(type: str, activity_id: int) -> str:
    return f"broadcast:pending:{type}:{activity_id}"


def send_throttled(type: str, room_id: int, activity_id: int, version: int) -> None:
    """Send `type` to the teacher at most once per THROTTLE_MS per activity, without ever
    dropping the last event of a burst.

    - Lock free (SET NX PX 500 succeeds): send now.
    - Locked, and no trailing event scheduled yet (SET NX on the pending flag succeeds):
      schedule one trailing task in THROTTLE_MS.
    - Locked and a trailing task is already scheduled: nothing to do; it will send the
      latest version. So a burst schedules at most one trailing task.
    """
    try:
        store = get_store()
        if store.set(_lock_key(type, activity_id), px=THROTTLE_MS, nx=True):
            send(teacher_group(room_id), event(type, activity_id, version))
            return
        if store.set(_pending_key(type, activity_id), px=PENDING_MS, nx=True):
            from .tasks import send_trailing_event

            send_trailing_event.apply_async(args=[type, activity_id], countdown=THROTTLE_MS / 1000)
    except Exception:
        logger.exception("Could not broadcast %s for activity %s", type, activity_id)


def send_trailing(type: str, activity_id: int) -> None:
    """The trailing event of a throttled burst (run by the Celery task).

    Clears the pending flag *before* reading the version: a write committing meanwhile is
    either included in the version read here or schedules its own trailing event.
    """
    from .models import Activity

    store = get_store()
    store.delete(_pending_key(type, activity_id))
    row = Activity.objects.filter(pk=activity_id).values("room_id", "version").first()
    if row is None:
        return  # deleted since; nobody to tell
    store.set(_lock_key(type, activity_id), px=THROTTLE_MS)
    send(teacher_group(row["room_id"]), event(type, activity_id, row["version"]))


# --- Helpers for services (call inside the transaction) ---------------------


def activity_started(activity) -> None:
    """New activity: students re-join and fetch state; the teacher refetches."""
    _after_commit(
        student_group(activity.room.code), event(ACTIVITY_STARTED, activity.id, activity.version)
    )
    activity_updated(activity, students=False)


def activity_updated(activity, *, students: bool = True) -> None:
    """Any activity change: the teacher refetches; with `students`, so do students
    (navigate and settings changes)."""
    if students:
        _after_commit(
            student_group(activity.room.code),
            event(ACTIVITY_UPDATED, activity.id, activity.version),
        )
    _after_commit(teacher_group(activity.room_id), event(ACTIVITY_UPDATED, activity.id, activity.version))


def activity_ended(activity) -> None:
    _after_commit(
        student_group(activity.room.code), event(ACTIVITY_ENDED, activity.id, activity.version)
    )
    activity_updated(activity, students=False)


def participant_removed(activity, participant_id) -> None:
    """The teacher removed a participant: that student goes back to the join screen."""
    _after_commit(
        student_group(activity.room.code),
        event(PARTICIPANT_REMOVED, activity.id, activity.version, participant_id=str(participant_id)),
    )
    participants_changed(activity)


def participants_changed(activity) -> None:
    room_id, activity_id, version = activity.room_id, activity.id, activity.version
    transaction.on_commit(
        lambda: send_throttled(PARTICIPANTS_CHANGED, room_id, activity_id, version)
    )


def responses_updated(activity) -> None:
    room_id, activity_id, version = activity.room_id, activity.id, activity.version
    transaction.on_commit(lambda: send_throttled(RESPONSES_UPDATED, room_id, activity_id, version))
