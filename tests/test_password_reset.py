from datetime import datetime, timedelta
from unittest import mock

import pytest
from django.contrib.auth.tokens import PasswordResetTokenGenerator, default_token_generator
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework_simplejwt.tokens import RefreshToken


from .conftest import TEACHER_PASSWORD, is_verified

pytestmark = pytest.mark.django_db

LOGIN = "/api/auth/login"
REFRESH = "/api/auth/refresh"
RESET = "/api/auth/password-reset"
CONFIRM = "/api/auth/password-reset/confirm"

NEW_PASSWORD = "a-brand-new-password"


def reset_params(user) -> dict:
    return {
        "uid": urlsafe_base64_encode(force_bytes(user.pk)),
        "token": default_token_generator.make_token(user),
    }


def confirm(api_client, params: dict, new_password: str = NEW_PASSWORD):
    return api_client.post(CONFIRM, {**params, "new_password": new_password}, format="json")


def assert_invalid_link(response):
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_or_expired_token"


# --- Request ----------------------------------------------------------------


def test_reset_request_emails_a_link(post_committed, brevo, teacher):
    response = post_committed(RESET, {"email": "Ada@Example.com"})

    assert response.status_code == 200
    assert len(brevo) == 1
    assert brevo.payloads[0]["subject"] == "Reset your Flashform password"
    path, params = brevo.last_link()
    assert path == "/reset-password"
    assert params == reset_params(teacher)


def test_reset_request_for_unknown_email_is_200_and_sends_nothing(post_committed, brevo):
    response = post_committed(RESET, {"email": "nobody@example.com"})

    assert response.status_code == 200
    assert response.json() == {
        "detail": "If an account exists for this email, we've sent a link to reset your password."
    }
    assert len(brevo) == 0


def test_reset_request_for_inactive_user_sends_nothing(post_committed, brevo, teacher):
    teacher.is_active = False
    teacher.save()

    assert post_committed(RESET, {"email": "ada@example.com"}).status_code == 200
    assert len(brevo) == 0


def test_reset_request_is_throttled_per_email(api_client, teacher):
    for _ in range(3):
        assert api_client.post(RESET, {"email": "ada@example.com"}, format="json").status_code == 200

    response = api_client.post(RESET, {"email": "ada@example.com"}, format="json")

    assert response.status_code == 429
    assert response.json()["code"] == "throttled"


def test_reset_request_is_throttled_per_ip(api_client):
    for i in range(10):
        assert api_client.post(RESET, {"email": f"u{i}@example.com"}, format="json").status_code == 200

    assert api_client.post(RESET, {"email": "fresh@example.com"}, format="json").status_code == 429


# --- Confirm ----------------------------------------------------------------


def test_emailed_link_resets_the_password(api_client, post_committed, brevo, teacher):
    post_committed(RESET, {"email": "ada@example.com"})
    _, params = brevo.last_link()

    response = confirm(api_client, params)

    assert response.status_code == 200
    assert response.json() == {"detail": "Your password has been reset. You can now log in."}
    old = api_client.post(LOGIN, {"email": "ada@example.com", "password": TEACHER_PASSWORD}, format="json")
    new = api_client.post(LOGIN, {"email": "ada@example.com", "password": NEW_PASSWORD}, format="json")
    assert (old.status_code, new.status_code) == (401, 200)


def test_reset_token_is_single_use(api_client, teacher):
    params = reset_params(teacher)
    assert confirm(api_client, params).status_code == 200

    # The token is bound to the old password hash, so it is dead now.
    assert_invalid_link(confirm(api_client, params, "yet-another-password"))
    teacher.refresh_from_db()
    assert teacher.check_password(NEW_PASSWORD)


def test_reset_token_expires_after_an_hour(api_client, teacher):
    params = reset_params(teacher)
    later = datetime.now() + timedelta(hours=1, minutes=1)

    with mock.patch.object(PasswordResetTokenGenerator, "_now", return_value=later):
        assert_invalid_link(confirm(api_client, params))


def test_reset_blacklists_all_refresh_tokens(api_client, teacher):
    refresh_tokens = [str(RefreshToken.for_user(teacher)) for _ in range(2)]

    assert confirm(api_client, reset_params(teacher)).status_code == 200

    for token in refresh_tokens:
        response = api_client.post(REFRESH, {"refresh": token}, format="json")
        assert response.status_code == 401
        assert response.json()["code"] == "token_not_valid"


def test_reset_marks_email_verified(api_client, unverified_teacher):
    assert confirm(api_client, reset_params(unverified_teacher)).status_code == 200

    assert is_verified(unverified_teacher) is True
    login = api_client.post(
        LOGIN, {"email": "ada@example.com", "password": NEW_PASSWORD}, format="json"
    )
    assert login.status_code == 200


def test_reset_validates_the_new_password(api_client, teacher):
    params = reset_params(teacher)

    response = confirm(api_client, params, "12345678")

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "validation_error"
    assert "new_password" in body["fields"]
    # Nothing changed, so the link still works.
    teacher.refresh_from_db()
    assert teacher.check_password(TEACHER_PASSWORD)
    assert confirm(api_client, params).status_code == 200


@pytest.mark.parametrize("uid", ["", "!!!", "bm90LWEtbnVtYmVy", urlsafe_base64_encode(b"999999")])
def test_reset_rejects_bad_uid(api_client, teacher, uid):
    params = {**reset_params(teacher), "uid": uid}

    response = confirm(api_client, params)

    if uid == "":
        assert response.status_code == 400  # required field
    else:
        assert_invalid_link(response)


def test_reset_rejects_bad_token(api_client, teacher):
    assert_invalid_link(confirm(api_client, {**reset_params(teacher), "token": "abc-def"}))


def test_reset_token_for_another_user_is_rejected(api_client, teacher, django_user_model):
    other = django_user_model.objects.create_user(username="bob@example.com", email="bob@example.com")
    params = {"uid": reset_params(other)["uid"], "token": reset_params(teacher)["token"]}

    assert_invalid_link(confirm(api_client, params))
