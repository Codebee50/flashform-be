"""Teacher accounts: registration, login, JWT lifecycle, email verification and password
reset (PRD §5.1, §8).

Teachers use Django's default User: `username` and `email` both hold the lowercased
email (so the unique `username` column enforces case-insensitive email uniqueness),
and `first_name` holds the display name. `TeacherProfile` holds `email_verified`.
"""

from datetime import timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.models import update_last_login
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import default_token_generator
from django.core import signing
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from rest_framework.exceptions import ValidationError
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.settings import api_settings as jwt_settings
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken

from .email_service import EmailService
from .exceptions import EmailNotVerified, InvalidCredentials, InvalidRefreshToken
from .models import TeacherProfile

User = get_user_model()

EMAIL_TAKEN_MESSAGE = "An account with this email already exists."
INVALID_LINK_MESSAGE = "This link is invalid or has expired."

VERIFY_EMAIL_SALT = "verify-email"
VERIFY_EMAIL_MAX_AGE = timedelta(days=3)


def normalize_email(email: str) -> str:
    return email.strip().lower()


def invalid_link() -> ValidationError:
    return ValidationError(INVALID_LINK_MESSAGE, code="invalid_or_expired_token")


def get_profile(user: User) -> TeacherProfile:
    """The user's profile, created on the fly for users that bypassed the post_save signal."""
    try:
        return user.profile
    except TeacherProfile.DoesNotExist:
        profile, _ = TeacherProfile.objects.get_or_create(user=user)
        user.profile = profile
        return profile


def find_active_teacher(email: str) -> User | None:
    return User.objects.filter(username=normalize_email(email), is_active=True).first()


# --- Registration and login -------------------------------------------------


def register_teacher(*, name: str, email: str, password: str) -> User:
    email = normalize_email(email)
    if User.objects.filter(username=email).exists():
        raise ValidationError({"email": [EMAIL_TAKEN_MESSAGE]}, code="email_taken")
    try:
        with transaction.atomic():
            user = User.objects.create_user(
                username=email, email=email, password=password, first_name=name
            )
            # Queued on commit, so the email never outlives a rolled-back user.
            send_verification_email(user)
    except IntegrityError:
        # Lost a race with a concurrent registration for the same email.
        raise ValidationError({"email": [EMAIL_TAKEN_MESSAGE]}, code="email_taken")
    return user


def authenticate_teacher(*, email: str, password: str) -> User:
    user = authenticate(username=normalize_email(email), password=password)
    if user is None:
        raise InvalidCredentials()
    # Checked only after the password, so this never reveals whether an account exists.
    if not get_profile(user).email_verified:
        raise EmailNotVerified()
    update_last_login(None, user)
    return user


# --- JWTs -------------------------------------------------------------------


def issue_tokens(user: User) -> dict[str, str]:
    refresh = RefreshToken.for_user(user)
    return {"access": str(refresh.access_token), "refresh": str(refresh)}


def rotate_refresh_token(raw_refresh: str) -> dict[str, str]:
    """Exchange a refresh token for a new access/refresh pair and blacklist the old one.

    Each refresh token can be used exactly once, even under concurrent requests.
    """
    try:
        refresh = RefreshToken(raw_refresh)  # checks signature, expiry, type and blacklist
    except TokenError:
        raise InvalidRefreshToken()

    user_id = refresh.payload.get(jwt_settings.USER_ID_CLAIM)
    user = User.objects.filter(**{jwt_settings.USER_ID_FIELD: user_id}, is_active=True).first()
    if user is None:
        raise InvalidRefreshToken()

    try:
        with transaction.atomic():
            _, created = refresh.blacklist()
    except IntegrityError:
        created = False
    if not created:
        # Another request rotated this token first.
        raise InvalidRefreshToken()

    return issue_tokens(user)


def revoke_refresh_token(raw_refresh: str) -> None:
    """Blacklist a refresh token. Idempotent: invalid or already revoked tokens are ignored."""
    try:
        with transaction.atomic():
            RefreshToken(raw_refresh).blacklist()
    except (TokenError, IntegrityError):
        pass


def revoke_all_refresh_tokens(user: User) -> None:
    """Blacklist every unexpired refresh token issued to `user` (log out everywhere)."""
    outstanding = OutstandingToken.objects.filter(user=user, expires_at__gt=timezone.now())
    BlacklistedToken.objects.bulk_create(
        [BlacklistedToken(token=token) for token in outstanding], ignore_conflicts=True
    )


# --- Email verification (PRD T4) --------------------------------------------


def make_verification_token(user: User) -> str:
    return signing.dumps({"uid": user.pk, "email": user.email}, salt=VERIFY_EMAIL_SALT)


def send_verification_email(user: User) -> None:
    link = f"{settings.FRONTEND_URL}/verify-email?{urlencode({'token': make_verification_token(user)})}"
    EmailService(user.email).send_template_mail(
        f"Verify your {settings.APP_NAME} email",
        "emails/verify_email.html",
        {"name": user.first_name, "action_url": link},
    )


def mark_email_verified(user: User) -> None:
    profile = get_profile(user)
    if not profile.email_verified:
        profile.email_verified = True
        profile.email_verified_at = timezone.now()
        profile.save(update_fields=["email_verified", "email_verified_at"])


def verify_email(token: str) -> User:
    """Mark the email in `token` verified. Idempotent for an already verified account."""
    try:
        data = signing.loads(token, salt=VERIFY_EMAIL_SALT, max_age=VERIFY_EMAIL_MAX_AGE)
    except signing.BadSignature:  # includes SignatureExpired
        raise invalid_link()
    if not isinstance(data, dict):
        raise invalid_link()

    user = User.objects.filter(pk=data.get("uid"), is_active=True).first()
    # The link proves ownership of the email it was sent to, not of the current one.
    if user is None or user.email != data.get("email"):
        raise invalid_link()

    mark_email_verified(user)
    return user


def resend_verification(email: str) -> None:
    """Send a new verification link if an active, unverified account exists. Silent otherwise."""
    user = find_active_teacher(email)
    if user is not None and not get_profile(user).email_verified:
        send_verification_email(user)


# --- Password reset (PRD T5) ------------------------------------------------


def request_password_reset(email: str) -> None:
    """Email a reset link if an active account exists. Silent otherwise."""
    user = find_active_teacher(email)
    if user is None or not user.has_usable_password():
        return
    query = urlencode(
        {
            "uid": urlsafe_base64_encode(force_bytes(user.pk)),
            "token": default_token_generator.make_token(user),
        }
    )
    EmailService(user.email).send_template_mail(
        f"Reset your {settings.APP_NAME} password",
        "emails/password_reset.html",
        {"name": user.first_name, "action_url": f"{settings.FRONTEND_URL}/reset-password?{query}"},
    )


def _user_pk_from_uid(uid: str) -> str | None:
    try:
        return urlsafe_base64_decode(uid).decode()
    except (TypeError, ValueError):  # includes binascii.Error and UnicodeDecodeError
        return None


def confirm_password_reset(*, uid: str, token: str, new_password: str) -> User:
    """Set a new password from a reset link, log out everywhere and mark the email verified.

    The token is bound to the password hash, so it stops working once the password changes.
    """
    pk = _user_pk_from_uid(uid)
    if pk is None:
        raise invalid_link()

    with transaction.atomic():
        # Lock the row so two concurrent requests can't both use the same token.
        try:
            user = User.objects.select_for_update().filter(pk=pk, is_active=True).first()
        except (TypeError, ValueError, OverflowError):
            user = None
        if user is None or not default_token_generator.check_token(user, token):
            raise invalid_link()

        try:
            validate_password(new_password, user=user)
        except DjangoValidationError as exc:
            raise ValidationError({"new_password": list(exc.messages)})

        user.set_password(new_password)
        user.save(update_fields=["password"])
        revoke_all_refresh_tokens(user)
        # They just proved they can read mail sent to this address.
        mark_email_verified(user)
    return user
