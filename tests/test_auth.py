from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from .conftest import TEACHER_PASSWORD

pytestmark = pytest.mark.django_db

REGISTER = "/api/auth/register"
LOGIN = "/api/auth/login"
REFRESH = "/api/auth/refresh"
LOGOUT = "/api/auth/logout"
ME = "/api/auth/me"


def bearer(token) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


# --- Register ---------------------------------------------------------------


def test_register_returns_user_and_message_without_tokens(api_client, django_user_model):
    response = api_client.post(
        REGISTER,
        {"name": "  Grace Hopper ", "email": "Grace@Example.COM ", "password": "a-long-password"},
        format="json",
    )

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"user", "detail"}
    assert "verify your email" in body["detail"]
    user = django_user_model.objects.get()
    assert body["user"] == {
        "id": user.id,
        "name": "Grace Hopper",
        "email": "grace@example.com",
        "email_verified": False,
    }
    assert user.username == user.email == "grace@example.com"
    assert user.first_name == "Grace Hopper"
    assert user.check_password("a-long-password")


def test_register_duplicate_email_is_case_insensitive(api_client, teacher):
    response = api_client.post(
        REGISTER,
        {"name": "Impostor", "email": "ADA@example.com", "password": "another-password"},
        format="json",
    )

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "validation_error"
    assert body["fields"] == {"email": ["An account with this email already exists."]}


def test_register_rejects_short_password(api_client, django_user_model):
    response = api_client.post(
        REGISTER, {"name": "Short", "email": "s@example.com", "password": "x7!kq"}, format="json"
    )

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "validation_error"
    assert "password" in body["fields"]
    assert "at least 8 characters" in body["detail"]
    assert not django_user_model.objects.exists()


def test_register_rejects_common_password(api_client):
    response = api_client.post(
        REGISTER, {"name": "Lazy", "email": "l@example.com", "password": "password123"}, format="json"
    )

    assert response.status_code == 400
    assert "too common" in response.json()["fields"]["password"][0]


def test_register_requires_all_fields(api_client):
    response = api_client.post(REGISTER, {}, format="json")

    assert response.status_code == 400
    assert set(response.json()["fields"]) == {"name", "email", "password"}


def test_register_rejects_invalid_email(api_client):
    response = api_client.post(
        REGISTER, {"name": "X", "email": "not-an-email", "password": "a-long-password"}, format="json"
    )

    assert response.status_code == 400
    assert "email" in response.json()["fields"]


# --- Login ------------------------------------------------------------------


def test_login_returns_tokens_and_user(api_client, teacher):
    response = api_client.post(
        LOGIN, {"email": " ADA@Example.com", "password": TEACHER_PASSWORD}, format="json"
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"access", "refresh", "user"}
    assert body["user"] == {
        "id": teacher.id,
        "name": "Ada Lovelace",
        "email": "ada@example.com",
        "email_verified": True,
    }
    assert api_client.get(ME, **bearer(body["access"])).status_code == 200


def test_login_wrong_password(api_client, teacher):
    response = api_client.post(
        LOGIN, {"email": "ada@example.com", "password": "wrong-password"}, format="json"
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid email or password.", "code": "invalid_credentials"}


def test_login_unknown_email_gives_same_error(api_client, teacher):
    response = api_client.post(
        LOGIN, {"email": "nobody@example.com", "password": TEACHER_PASSWORD}, format="json"
    )

    assert response.status_code == 401
    assert response.json()["code"] == "invalid_credentials"


def test_login_inactive_user_rejected(api_client, teacher):
    teacher.is_active = False
    teacher.save()

    response = api_client.post(
        LOGIN, {"email": "ada@example.com", "password": TEACHER_PASSWORD}, format="json"
    )

    assert response.status_code == 401


def test_login_is_throttled(api_client, teacher):
    payload = {"email": "ada@example.com", "password": "wrong-password"}
    for _ in range(10):
        assert api_client.post(LOGIN, payload, format="json").status_code == 401

    response = api_client.post(LOGIN, payload, format="json")

    assert response.status_code == 429
    assert response.json()["code"] == "throttled"
    assert "Retry-After" in response.headers


# --- Me / access tokens -----------------------------------------------------


def test_me_returns_current_user(teacher_client, teacher):
    response = teacher_client.get(ME)

    assert response.status_code == 200
    assert response.json() == {
        "id": teacher.id,
        "name": "Ada Lovelace",
        "email": "ada@example.com",
        "email_verified": True,
    }


def test_me_without_token(api_client):
    response = api_client.get(ME)

    assert response.status_code == 401
    assert response.json()["code"] == "not_authenticated"


def test_me_with_garbage_token(api_client):
    response = api_client.get(ME, **bearer("not.a.jwt"))

    assert response.status_code == 401
    assert response.json()["code"] == "token_not_valid"


def test_me_with_expired_token(api_client, teacher):
    token = AccessToken.for_user(teacher)
    token.set_exp(from_time=timezone.now() - timedelta(hours=1))

    response = api_client.get(ME, **bearer(token))

    assert response.status_code == 401
    assert response.json()["code"] == "token_not_valid"


def test_me_rejects_refresh_token_as_access(api_client, teacher):
    response = api_client.get(ME, **bearer(RefreshToken.for_user(teacher)))

    assert response.status_code == 401


# --- Refresh ----------------------------------------------------------------


def login(api_client) -> dict:
    response = api_client.post(
        LOGIN, {"email": "ada@example.com", "password": TEACHER_PASSWORD}, format="json"
    )
    assert response.status_code == 200
    return response.json()


def test_refresh_returns_new_pair(api_client, teacher):
    tokens = login(api_client)

    response = api_client.post(REFRESH, {"refresh": tokens["refresh"]}, format="json")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"access", "refresh"}
    assert body["refresh"] != tokens["refresh"]
    assert body["access"] != tokens["access"]
    assert api_client.get(ME, **bearer(body["access"])).status_code == 200


def test_old_refresh_token_rejected_after_rotation(api_client, teacher):
    old_refresh = login(api_client)["refresh"]
    assert api_client.post(REFRESH, {"refresh": old_refresh}, format="json").status_code == 200

    response = api_client.post(REFRESH, {"refresh": old_refresh}, format="json")

    assert response.status_code == 401
    assert response.json()["code"] == "token_not_valid"


def test_rotated_refresh_token_can_itself_be_rotated(api_client, teacher):
    first = login(api_client)["refresh"]
    second = api_client.post(REFRESH, {"refresh": first}, format="json").json()["refresh"]

    response = api_client.post(REFRESH, {"refresh": second}, format="json")

    assert response.status_code == 200


def test_refresh_with_garbage_token(api_client):
    response = api_client.post(REFRESH, {"refresh": "garbage"}, format="json")

    assert response.status_code == 401
    assert response.json()["code"] == "token_not_valid"


def test_refresh_rejects_access_token(api_client, teacher):
    access = login(api_client)["access"]

    response = api_client.post(REFRESH, {"refresh": access}, format="json")

    assert response.status_code == 401


def test_refresh_ignores_stale_authorization_header(api_client, teacher):
    # The frontend refreshes precisely because its access token expired.
    tokens = login(api_client)

    response = api_client.post(
        REFRESH, {"refresh": tokens["refresh"]}, format="json", **bearer("expired.or.garbage")
    )

    assert response.status_code == 200


def test_refresh_rejected_for_deactivated_user(api_client, teacher):
    tokens = login(api_client)
    teacher.is_active = False
    teacher.save()

    response = api_client.post(REFRESH, {"refresh": tokens["refresh"]}, format="json")

    assert response.status_code == 401


def test_refresh_requires_token(api_client):
    response = api_client.post(REFRESH, {}, format="json")

    assert response.status_code == 400
    assert "refresh" in response.json()["fields"]


# --- Logout -----------------------------------------------------------------


def test_logout_blacklists_refresh_token(api_client, teacher):
    tokens = login(api_client)

    response = api_client.post(LOGOUT, {"refresh": tokens["refresh"]}, format="json")

    assert response.status_code == 204
    refresh = api_client.post(REFRESH, {"refresh": tokens["refresh"]}, format="json")
    assert refresh.status_code == 401
    assert refresh.json()["code"] == "token_not_valid"


def test_logout_is_idempotent(api_client, teacher):
    tokens = login(api_client)

    first = api_client.post(LOGOUT, {"refresh": tokens["refresh"]}, format="json")
    second = api_client.post(LOGOUT, {"refresh": tokens["refresh"]}, format="json")
    garbage = api_client.post(LOGOUT, {"refresh": "garbage"}, format="json")

    assert (first.status_code, second.status_code, garbage.status_code) == (204, 204, 204)


def test_logout_requires_token(api_client):
    response = api_client.post(LOGOUT, {}, format="json")

    assert response.status_code == 400
