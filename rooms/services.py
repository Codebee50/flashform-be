"""Rooms: creation with generated or custom codes, rename/lock, delete, public lookup
(PRD §5.2, §7, §8).

Codes are stored uppercase. Generated codes are 6 characters from CODE_ALPHABET (no
ambiguous `0 O 1 I L`, PRD §17); custom codes are 4–10 characters of `A–Z 0–9`.
"""

import re
import secrets

from django.contrib.auth.models import AbstractBaseUser
from django.db import IntegrityError, transaction
from django.db.models import Prefetch, QuerySet
from rest_framework.exceptions import NotFound, ValidationError

from .models import Room

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 6
# 31^6 ≈ 887 million codes: a collision is rare and ten in a row practically impossible.
MAX_CODE_ATTEMPTS = 10

CUSTOM_CODE_RE = re.compile(r"[A-Z0-9]{4,10}")
CUSTOM_CODE_MESSAGE = "Room code must be 4–10 characters, letters A–Z and digits 0–9 only."
CODE_TAKEN_MESSAGE = "This room code is already taken."


def generate_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def normalize_code(code: str) -> str:
    """How students type codes: case-insensitive, spaces ignored (PRD S1)."""
    return re.sub(r"\s+", "", code).upper()


def validate_custom_code(code: str) -> str:
    """Uppercase a teacher-chosen code, or raise if it isn't 4–10 chars of A–Z 0–9."""
    code = code.strip().upper()
    if not CUSTOM_CODE_RE.fullmatch(code):
        raise ValidationError({"code": [CUSTOM_CODE_MESSAGE]}, code="invalid_code")
    return code


def with_live_activity(rooms: QuerySet[Room]) -> QuerySet[Room]:
    """Prefetch each room's LIVE activity for `Room.live_activity` (one extra query total)."""
    from activities.models import Activity, ActivityStatus

    return rooms.prefetch_related(
        Prefetch(
            "activities",
            queryset=Activity.objects.filter(status=ActivityStatus.LIVE),
            to_attr="prefetched_live_activities",
        )
    )


def owned_rooms(owner: AbstractBaseUser) -> QuerySet[Room]:
    """The teacher's rooms. Other teachers' rooms are simply absent, so lookups 404."""
    return with_live_activity(Room.objects.filter(owner=owner))


def create_room(*, owner: AbstractBaseUser, name: str, code: str | None = None) -> Room:
    if code:
        code = validate_custom_code(code)
        try:
            with transaction.atomic():
                return Room.objects.create(owner=owner, name=name, code=code)
        except IntegrityError:
            raise ValidationError({"code": [CODE_TAKEN_MESSAGE]}, code="code_taken")

    for _ in range(MAX_CODE_ATTEMPTS):
        try:
            # A savepoint per attempt, so a collision doesn't break an outer transaction.
            with transaction.atomic():
                return Room.objects.create(owner=owner, name=name, code=generate_code())
        except IntegrityError:
            continue
    raise RuntimeError(f"Could not generate a unique room code in {MAX_CODE_ATTEMPTS} attempts.")


def update_room(room: Room, *, name: str | None = None, is_locked: bool | None = None) -> Room:
    """Rename and/or lock/unlock. Lock enforcement happens when students join."""
    changed = []
    if name is not None:
        room.name = name
        changed.append("name")
    if is_locked is not None:
        room.is_locked = is_locked
        changed.append("is_locked")
    if changed:
        room.save(update_fields=[*changed, "updated_at"])
    return room


def delete_room(room: Room) -> None:
    """Deletes the room; its activities and responses cascade (PRD R3)."""
    room.delete()


def get_public_room(code: str) -> Room:
    """A room by the code a student typed, for the no-auth student lookup."""
    try:
        return Room.objects.get(code=normalize_code(code))
    except Room.DoesNotExist:
        raise NotFound("Room not found.", code="room_not_found")
