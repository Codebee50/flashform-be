from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from flashform.serializers import ErrorSerializer

from . import services
from .serializers import (
    AuthResponseSerializer,
    DetailSerializer,
    EmailSerializer,
    LoginSerializer,
    PasswordResetConfirmSerializer,
    RefreshSerializer,
    RegisterResponseSerializer,
    RegisterSerializer,
    TokenPairSerializer,
    UserSerializer,
    VerifyEmailSerializer,
)
from .throttles import EmailAddressRateThrottle


class PublicAuthView(APIView):
    """Token endpoints need no JWT, so a stale access token header never gets in the way."""

    authentication_classes = []
    permission_classes = [AllowAny]


def auth_payload(user) -> dict:
    return AuthResponseSerializer({**services.issue_tokens(user), "user": user}).data


def detail(message: str) -> dict:
    return DetailSerializer({"detail": message}).data


class RegisterView(PublicAuthView):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_register"

    @extend_schema(
        operation_id="auth_register",
        tags=["auth"],
        request=RegisterSerializer,
        responses={201: RegisterResponseSerializer, 400: ErrorSerializer, 429: ErrorSerializer},
        description="Creates the account and emails a verification link. Returns no tokens: "
        "login answers 403 `email_not_verified` until the link is used.",
    )
    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = services.register_teacher(**serializer.validated_data)
        payload = RegisterResponseSerializer(
            {
                "user": user,
                "detail": "Account created. Check your inbox for a link to verify your email.",
            }
        ).data
        return Response(payload, status=status.HTTP_201_CREATED)


class LoginView(PublicAuthView):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_login"

    @extend_schema(
        operation_id="auth_login",
        tags=["auth"],
        request=LoginSerializer,
        responses={
            200: AuthResponseSerializer,
            400: ErrorSerializer,
            401: ErrorSerializer,
            403: ErrorSerializer,
            429: ErrorSerializer,
        },
        description="403 `email_not_verified` when the password is right but the email "
        "is not verified yet.",
    )
    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = services.authenticate_teacher(**serializer.validated_data)
        return Response(auth_payload(user))


class RefreshView(PublicAuthView):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_refresh"

    @extend_schema(
        operation_id="auth_refresh",
        tags=["auth"],
        request=RefreshSerializer,
        responses={
            200: TokenPairSerializer,
            400: ErrorSerializer,
            401: ErrorSerializer,
            429: ErrorSerializer,
        },
    )
    def post(self, request):
        serializer = RefreshSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        tokens = services.rotate_refresh_token(serializer.validated_data["refresh"])
        return Response(TokenPairSerializer(tokens).data)


class LogoutView(PublicAuthView):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_logout"

    @extend_schema(
        operation_id="auth_logout",
        tags=["auth"],
        request=RefreshSerializer,
        responses={204: None, 400: ErrorSerializer, 429: ErrorSerializer},
    )
    def post(self, request):
        serializer = RefreshSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.revoke_refresh_token(serializer.validated_data["refresh"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class VerifyEmailView(PublicAuthView):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_token"

    @extend_schema(
        operation_id="auth_verify_email",
        tags=["auth"],
        request=VerifyEmailSerializer,
        responses={200: DetailSerializer, 400: ErrorSerializer, 429: ErrorSerializer},
        description="`token` comes from the emailed link `{FRONTEND_URL}/verify-email?token=...`. "
        "Idempotent. 400 `invalid_or_expired_token` for a bad, expired (3 days) or stale link.",
    )
    def post(self, request):
        serializer = VerifyEmailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.verify_email(serializer.validated_data["token"])
        return Response(detail("Email verified. You can now log in."))


class ResendVerificationView(PublicAuthView):
    throttle_classes = [ScopedRateThrottle, EmailAddressRateThrottle]
    throttle_scope = "auth_resend_verification"

    @extend_schema(
        operation_id="auth_resend_verification",
        tags=["auth"],
        request=EmailSerializer,
        responses={200: DetailSerializer, 400: ErrorSerializer, 429: ErrorSerializer},
        description="Always 200, whether or not the email belongs to an unverified account.",
    )
    def post(self, request):
        serializer = EmailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.resend_verification(serializer.validated_data["email"])
        return Response(
            detail(
                "If an unverified account exists for this email, "
                "we've sent a new verification link."
            )
        )


class PasswordResetView(PublicAuthView):
    throttle_classes = [ScopedRateThrottle, EmailAddressRateThrottle]
    throttle_scope = "auth_password_reset"

    @extend_schema(
        operation_id="auth_password_reset",
        tags=["auth"],
        request=EmailSerializer,
        responses={200: DetailSerializer, 400: ErrorSerializer, 429: ErrorSerializer},
        description="Always 200, whether or not an account exists for the email.",
    )
    def post(self, request):
        serializer = EmailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.request_password_reset(serializer.validated_data["email"])
        return Response(
            detail("If an account exists for this email, we've sent a link to reset your password.")
        )


class PasswordResetConfirmView(PublicAuthView):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_token"

    @extend_schema(
        operation_id="auth_password_reset_confirm",
        tags=["auth"],
        request=PasswordResetConfirmSerializer,
        responses={200: DetailSerializer, 400: ErrorSerializer, 429: ErrorSerializer},
        description="`uid` and `token` come from `{FRONTEND_URL}/reset-password?uid=...&token=...` "
        "(valid 1 hour, single use). 400 `invalid_or_expired_token`, or `validation_error` with "
        "`fields.new_password`. Logs the teacher out everywhere and marks the email verified.",
    )
    def post(self, request):
        serializer = PasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.confirm_password_reset(**serializer.validated_data)
        return Response(detail("Your password has been reset. You can now log in."))


class MeView(APIView):
    @extend_schema(
        operation_id="auth_me",
        tags=["auth"],
        responses={200: UserSerializer, 401: ErrorSerializer},
    )
    def get(self, request):
        return Response(UserSerializer(request.user).data)
