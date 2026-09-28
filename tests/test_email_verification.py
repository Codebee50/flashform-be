import time
from unittest import mock

import pytest
from django.core.management import CommandError, call_command

from accounts.models import TeacherProfile
from accounts.services import VERIFY_EMAIL_MAX_AGE, get_profile, make_verification_token

from .conftest import TEACHER_PASSWORD, is_verified

pytestmark = pytest.mark.django_db

REGISTER = "/api/auth/register"
LOGIN = "/api/auth/login"
ME = "/api/auth/me"
VERIFY = "/api/auth/verify-email"
RESEND = "/api/auth/resend-verification"

ADA_LOGIN = {"email": "ada@example.com", "password": TEACHER_PASSWORD}


def token_created_ago(user, seconds: float) -> str:
    with mock.patch("django.core.signing.time.time", return_value=time.time() - seconds):
        return make_verification_token(user)


def assert_invalid_link(response):
    assert response.status_code == 400
    assert response.json() == {
        "detail": "This link is invalid or has expired.",
        "code": "invalid_or_expired_token",
    }


# --- Profile ----------------------------------------------------------------


def test_every_new_user_gets_an_unverified_profile(django_user_model):
    user = django_user_model.objects.create_user(username="x@example.com", password="pw")

    assert user.profile.email_verified is False
    assert user.profile.email_verified_at is None


def test_get_profile_creates_a_missing_profile(unverified_teacher):
    TeacherProfile.objects.filter(user=unverified_teacher).delete()
    unverified_teacher.refresh_from_db()

    assert get_profile(unverified_teacher).email_verified is False
    assert TeacherProfile.objects.filter(user=unverified_teacher).count() == 1


# --- Register ---------------------------------------------------------------


def test_register_sends_one_verification_email_and_no_tokens(post_committed, brevo):
    response = post_committed(
        REGISTER, {"name": "Grace", "email": "Grace@Example.com", "password": "a-long-password"}
    )

    assert response.status_code == 201
    assert "access" not in response.json() and "refresh" not in response.json()
    assert len(brevo) == 1
    [payload] = brevo.payloads
    assert payload["to"] == [{"email": "grace@example.com"}]
    assert payload["subject"] == "Verify your Flashform email"
    path, params = brevo.last_link()
    assert path == "/verify-email"
    assert set(params) == {"token"}


def test_failed_register_sends_nothing(post_committed, brevo, teacher):
    response = post_committed(
        REGISTER, {"name": "Dup", "email": "ada@example.com", "password": "a-long-password"}
    )

    assert response.status_code == 400
    assert len(brevo) == 0


# --- Login gate -------------------------------------------------------------


def test_unverified_login_is_refused_with_403(api_client, unverified_teacher):
    response = api_client.post(LOGIN, ADA_LOGIN, format="json")

    assert response.status_code == 403
    assert response.json() == {
        "detail": "Please verify your email address before logging in.",
        "code": "email_not_verified",
    }


def test_unverified_login_with_wrong_password_is_still_401(api_client, unverified_teacher):
    # The 403 only comes after the password is confirmed, so it never reveals the account.
    response = api_client.post(
        LOGIN, {"email": "ada@example.com", "password": "wrong-password"}, format="json"
    )

    assert response.status_code == 401
    assert response.json()["code"] == "invalid_credentials"


# --- Verify -----------------------------------------------------------------


def test_register_verify_login_flow(api_client, post_committed, brevo):
    post_committed(REGISTER, {"name": "Grace", "email": "grace@example.com", "password": "a-long-password"})
    _, params = brevo.last_link()

    response = api_client.post(VERIFY, {"token": params["token"]}, format="json")

    assert response.status_code == 200
    assert response.json() == {"detail": "Email verified. You can now log in."}
    login = api_client.post(
        LOGIN, {"email": "grace@example.com", "password": "a-long-password"}, format="json"
    )
    assert login.status_code == 200
    assert login.json()["user"]["email_verified"] is True
    me = api_client.get(ME, HTTP_AUTHORIZATION=f"Bearer {login.json()['access']}")
    assert me.json()["email_verified"] is True


def test_verify_sets_verified_at(api_client, unverified_teacher):
    api_client.post(VERIFY, {"token": make_verification_token(unverified_teacher)}, format="json")

    unverified_teacher.profile.refresh_from_db()
    assert unverified_teacher.profile.email_verified is True
    assert unverified_teacher.profile.email_verified_at is not None


def test_verify_is_idempotent(api_client, unverified_teacher):
    token = make_verification_token(unverified_teacher)

    first = api_client.post(VERIFY, {"token": token}, format="json")
    second = api_client.post(VERIFY, {"token": token}, format="json")

    assert (first.status_code, second.status_code) == (200, 200)


def test_verify_token_is_valid_for_just_under_3_days(api_client, unverified_teacher):
    token = token_created_ago(unverified_teacher, VERIFY_EMAIL_MAX_AGE.total_seconds() - 60)

    assert api_client.post(VERIFY, {"token": token}, format="json").status_code == 200


def test_verify_expired_token(api_client, unverified_teacher):
    token = token_created_ago(unverified_teacher, VERIFY_EMAIL_MAX_AGE.total_seconds() + 60)

    assert_invalid_link(api_client.post(VERIFY, {"token": token}, format="json"))
    assert is_verified(unverified_teacher) is False


def test_verify_tampered_token(api_client, unverified_teacher):
    token = make_verification_token(unverified_teacher)
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")

    assert_invalid_link(api_client.post(VERIFY, {"token": tampered}, format="json"))


def test_verify_garbage_token(api_client):
    assert_invalid_link(api_client.post(VERIFY, {"token": "not-a-token"}, format="json"))


def test_verify_rejects_token_for_old_email(api_client, unverified_teacher):
    token = make_verification_token(unverified_teacher)
    unverified_teacher.email = unverified_teacher.username = "new@example.com"
    unverified_teacher.save()

    assert_invalid_link(api_client.post(VERIFY, {"token": token}, format="json"))
    assert is_verified(unverified_teacher) is False


def test_verify_rejects_inactive_user(api_client, unverified_teacher):
    token = make_verification_token(unverified_teacher)
    unverified_teacher.is_active = False
    unverified_teacher.save()

    assert_invalid_link(api_client.post(VERIFY, {"token": token}, format="json"))


def test_verify_requires_token(api_client):
    response = api_client.post(VERIFY, {}, format="json")

    assert response.status_code == 400
    assert "token" in response.json()["fields"]


# --- Resend -----------------------------------------------------------------


def test_resend_for_unverified_account_sends_a_new_link(post_committed, brevo, unverified_teacher):
    response = post_committed(RESEND, {"email": " ADA@example.com "})

    assert response.status_code == 200
    assert len(brevo) == 1
    assert brevo.payloads[0]["to"] == [{"email": "ada@example.com"}]


def test_resend_for_unknown_email_is_200_and_sends_nothing(post_committed, brevo):
    response = post_committed(RESEND, {"email": "nobody@example.com"})

    assert response.status_code == 200
    assert response.json()["detail"].startswith("If an unverified account exists")
    assert len(brevo) == 0


def test_resend_for_verified_account_is_200_and_sends_nothing(post_committed, brevo, teacher):
    response = post_committed(RESEND, {"email": "ada@example.com"})

    assert response.status_code == 200
    assert len(brevo) == 0


def test_resend_rejects_invalid_email(api_client):
    response = api_client.post(RESEND, {"email": "nope"}, format="json")

    assert response.status_code == 400
    assert "email" in response.json()["fields"]


def test_resend_is_throttled_per_email(api_client):
    for _ in range(3):
        assert api_client.post(RESEND, {"email": "a@example.com"}, format="json").status_code == 200

    # Case and whitespace don't dodge the limit.
    response = api_client.post(RESEND, {"email": " A@EXAMPLE.com"}, format="json")

    assert response.status_code == 429
    assert response.json()["code"] == "throttled"
    assert api_client.post(RESEND, {"email": "b@example.com"}, format="json").status_code == 200


def test_resend_is_throttled_per_ip(api_client):
    for i in range(10):
        assert api_client.post(RESEND, {"email": f"u{i}@example.com"}, format="json").status_code == 200

    response = api_client.post(RESEND, {"email": "fresh@example.com"}, format="json")

    assert response.status_code == 429


# --- Management command -----------------------------------------------------


def test_verify_teacher_command(unverified_teacher):
    call_command("verify_teacher", "ADA@example.com")

    assert is_verified(unverified_teacher) is True


def test_verify_teacher_command_unknown_email(db):
    with pytest.raises(CommandError, match="No teacher"):
        call_command("verify_teacher", "nobody@example.com")
