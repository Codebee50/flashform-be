from unittest import mock

import pytest
from django.db import IntegrityError, transaction
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from rooms import services
from rooms.models import Room

pytestmark = pytest.mark.django_db

ROOMS = "/api/rooms"


def room_url(room) -> str:
    return f"{ROOMS}/{room.id}"


def public_url(code: str) -> str:
    return f"{ROOMS}/{code}/public"


@pytest.fixture
def other_teacher(django_user_model):
    from accounts.services import mark_email_verified

    user = django_user_model.objects.create_user(
        username="bob@example.com", email="bob@example.com", password="x", first_name="Bob"
    )
    mark_email_verified(user)
    return user


@pytest.fixture
def other_client(other_teacher):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {AccessToken.for_user(other_teacher)}")
    return client


@pytest.fixture
def room(teacher):
    return Room.objects.create(owner=teacher, name="Period 3 Biology", code="BIO3AB")


# --- Code generation --------------------------------------------------------


def test_code_alphabet_matches_prd_and_has_no_ambiguous_characters():
    assert services.CODE_ALPHABET == "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    assert not set("0O1IL") & set(services.CODE_ALPHABET)


def test_generated_codes_use_only_the_alphabet():
    codes = {services.generate_code() for _ in range(2000)}

    assert all(len(code) == 6 for code in codes)
    assert set("".join(codes)) <= set(services.CODE_ALPHABET)
    # Random enough that 2000 draws from 31^6 codes practically never repeat.
    assert len(codes) > 1990


def test_create_room_generates_a_code(teacher_client, teacher):
    response = teacher_client.post(ROOMS, {"name": "  Period 3 Biology "}, format="json")

    assert response.status_code == 201
    body = response.json()
    room = Room.objects.get()
    assert body == {
        "id": room.id,
        "name": "Period 3 Biology",
        "code": room.code,
        "is_locked": False,
        "live_activity": None,
        "created_at": body["created_at"],
        "updated_at": body["updated_at"],
    }
    assert room.owner == teacher
    assert len(room.code) == 6
    assert set(room.code) <= set(services.CODE_ALPHABET)


@pytest.mark.parametrize("code", [None, "", "   "])
def test_empty_custom_code_means_generated(teacher_client, code):
    response = teacher_client.post(ROOMS, {"name": "Algebra", "code": code}, format="json")

    assert response.status_code == 201
    assert len(response.json()["code"]) == 6


def test_generated_codes_are_unique_across_rooms(teacher, other_teacher):
    rooms = [services.create_room(owner=teacher, name=f"Room {i}") for i in range(50)]
    rooms += [services.create_room(owner=other_teacher, name=f"Room {i}") for i in range(50)]

    assert len({room.code for room in rooms}) == 100


def test_generated_code_collision_is_retried(teacher, other_teacher):
    Room.objects.create(owner=other_teacher, name="Taken", code="AAAAAA")

    with mock.patch.object(services, "generate_code", side_effect=["AAAAAA", "AAAAAA", "BBBBBB"]):
        room = services.create_room(owner=teacher, name="Mine")

    assert room.code == "BBBBBB"
    assert Room.objects.count() == 2


def test_generated_code_collision_retry_keeps_outer_transaction_usable(teacher):
    Room.objects.create(owner=teacher, name="Taken", code="AAAAAA")

    with transaction.atomic():
        with mock.patch.object(services, "generate_code", side_effect=["AAAAAA", "CCCCCC"]):
            services.create_room(owner=teacher, name="Mine")
        # Still usable: the failed insert was rolled back to its savepoint only.
        assert Room.objects.filter(code="CCCCCC").exists()


def test_generated_code_gives_up_after_max_attempts(teacher):
    Room.objects.create(owner=teacher, name="Taken", code="AAAAAA")

    with mock.patch.object(services, "generate_code", return_value="AAAAAA") as generate:
        with pytest.raises(RuntimeError):
            services.create_room(owner=teacher, name="Mine")

    assert generate.call_count == services.MAX_CODE_ATTEMPTS
    assert Room.objects.count() == 1


def test_database_rejects_lowercase_codes(teacher):
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Room.objects.create(owner=teacher, name="Sneaky", code="abcdef")


# --- Custom codes -----------------------------------------------------------


@pytest.mark.parametrize(
    "given, stored",
    [
        ("math7b", "MATH7B"),
        ("  Bio3 ", "BIO3"),
        ("ABCDEFGHIJ", "ABCDEFGHIJ"),
        # Custom codes may use the characters generated codes avoid.
        ("OIL0", "OIL0"),
    ],
)
def test_custom_code_is_uppercased(teacher_client, given, stored):
    response = teacher_client.post(ROOMS, {"name": "Math", "code": given}, format="json")

    assert response.status_code == 201
    assert response.json()["code"] == stored
    assert Room.objects.get().code == stored


@pytest.mark.parametrize(
    "code",
    ["ABC", "ABCDEFGHIJK", "MATH-7", "MATH 7B", "ÄBCD", "math_7"],
)
def test_invalid_custom_code_is_rejected(teacher_client, code):
    response = teacher_client.post(ROOMS, {"name": "Math", "code": code}, format="json")

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "validation_error"
    assert body["fields"] == {"code": [services.CUSTOM_CODE_MESSAGE]}
    assert not Room.objects.exists()


@pytest.mark.parametrize("code", ["BIO3AB", "bio3ab", "Bio3Ab"])
def test_custom_code_collision_is_case_insensitive(teacher_client, room, code):
    response = teacher_client.post(ROOMS, {"name": "Copy", "code": code}, format="json")

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "validation_error"
    assert body["fields"] == {"code": ["This room code is already taken."]}
    assert Room.objects.count() == 1


def test_custom_code_collides_with_another_teachers_room(other_client, room):
    response = other_client.post(ROOMS, {"name": "Copy", "code": "bio3ab"}, format="json")

    assert response.status_code == 400
    assert response.json()["fields"] == {"code": ["This room code is already taken."]}


def test_create_room_requires_a_name(teacher_client):
    response = teacher_client.post(ROOMS, {"name": "   "}, format="json")

    assert response.status_code == 400
    assert "name" in response.json()["fields"]


def test_room_name_is_limited_to_100_characters(teacher_client):
    response = teacher_client.post(ROOMS, {"name": "x" * 101}, format="json")

    assert response.status_code == 400
    assert "name" in response.json()["fields"]


# --- List / retrieve / update / delete --------------------------------------


def test_list_shows_only_own_rooms_newest_first(teacher_client, teacher, other_teacher):
    older = services.create_room(owner=teacher, name="Older")
    newer = services.create_room(owner=teacher, name="Newer")
    services.create_room(owner=other_teacher, name="Not mine")

    response = teacher_client.get(ROOMS)

    assert response.status_code == 200
    body = response.json()
    assert [r["id"] for r in body] == [newer.id, older.id]
    assert body[0]["code"] == newer.code
    assert body[0]["live_activity"] is None


def test_retrieve_own_room(teacher_client, room):
    response = teacher_client.get(room_url(room))

    assert response.status_code == 200
    assert response.json()["code"] == "BIO3AB"


def test_rename_and_lock(teacher_client, room):
    response = teacher_client.patch(
        room_url(room), {"name": "Period 4 Biology", "is_locked": True}, format="json"
    )

    assert response.status_code == 200
    assert response.json()["name"] == "Period 4 Biology"
    assert response.json()["is_locked"] is True
    room.refresh_from_db()
    assert room.name == "Period 4 Biology"
    assert room.is_locked is True


def test_unlock_leaves_name_alone(teacher_client, room):
    room.is_locked = True
    room.save()

    response = teacher_client.patch(room_url(room), {"is_locked": False}, format="json")

    assert response.status_code == 200
    room.refresh_from_db()
    assert room.is_locked is False
    assert room.name == "Period 3 Biology"


def test_patch_cannot_change_the_code(teacher_client, room):
    response = teacher_client.patch(room_url(room), {"code": "NEWCODE"}, format="json")

    assert response.status_code == 200
    room.refresh_from_db()
    assert room.code == "BIO3AB"


def test_patch_rejects_a_blank_name(teacher_client, room):
    response = teacher_client.patch(room_url(room), {"name": ""}, format="json")

    assert response.status_code == 400
    assert "name" in response.json()["fields"]


def test_put_is_not_allowed(teacher_client, room):
    response = teacher_client.put(room_url(room), {"name": "x", "is_locked": False}, format="json")

    assert response.status_code == 405


def test_delete_room(teacher_client, room):
    response = teacher_client.delete(room_url(room))

    assert response.status_code == 204
    assert not Room.objects.exists()
    assert teacher_client.get(room_url(room)).status_code == 404


def test_deleting_the_teacher_deletes_their_rooms(teacher, room):
    teacher.delete()

    assert not Room.objects.exists()


# --- Ownership --------------------------------------------------------------


@pytest.mark.parametrize(
    "method, data",
    [
        ("get", None),
        ("patch", {"name": "Hijacked", "is_locked": True}),
        ("delete", None),
    ],
)
def test_other_teacher_gets_404(other_client, room, method, data):
    response = getattr(other_client, method)(room_url(room), data, format="json")

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    room.refresh_from_db()
    assert room.name == "Period 3 Biology"
    assert room.is_locked is False


def test_unknown_room_is_404(teacher_client):
    assert teacher_client.get(f"{ROOMS}/999999").status_code == 404


@pytest.mark.parametrize(
    "method, path",
    [("get", ROOMS), ("post", ROOMS), ("get", f"{ROOMS}/1"), ("patch", f"{ROOMS}/1"),
     ("delete", f"{ROOMS}/1")],
)
def test_rooms_require_authentication(api_client, method, path):
    response = getattr(api_client, method)(path, {}, format="json")

    assert response.status_code == 401
    assert response.json()["code"] == "not_authenticated"


# --- Public lookup (student) ------------------------------------------------


def test_public_room(api_client, room):
    response = api_client.get(public_url("BIO3AB"))

    assert response.status_code == 200
    assert response.json() == {
        "code": "BIO3AB",
        "name": "Period 3 Biology",
        "is_locked": False,
        "live_activity_id": None,
    }


@pytest.mark.parametrize("typed", ["bio3ab", "Bio3Ab", "BIO 3AB", " bio3ab "])
def test_public_room_code_is_case_insensitive_and_ignores_spaces(api_client, room, typed):
    response = api_client.get(public_url(typed))

    assert response.status_code == 200
    assert response.json()["code"] == "BIO3AB"


def test_public_room_shows_lock(api_client, room):
    room.is_locked = True
    room.save()

    response = api_client.get(public_url("BIO3AB"))

    assert response.status_code == 200
    assert response.json()["is_locked"] is True


def test_public_room_not_found(api_client, room):
    response = api_client.get(public_url("NOPE42"))

    assert response.status_code == 404
    assert response.json() == {"detail": "Room not found.", "code": "room_not_found"}


def test_public_room_ignores_a_stale_teacher_token(api_client, room):
    api_client.credentials(HTTP_AUTHORIZATION="Bearer not-a-real-token")

    assert api_client.get(public_url("BIO3AB")).status_code == 200
