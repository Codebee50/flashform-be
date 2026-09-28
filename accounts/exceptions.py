from rest_framework import status
from rest_framework.exceptions import APIException

# These deliberately do not subclass AuthenticationFailed: DRF turns that into a 403
# on views without authentication classes (login/refresh), and we want 401.


class InvalidCredentials(APIException):
    status_code = status.HTTP_401_UNAUTHORIZED
    default_detail = "Invalid email or password."
    default_code = "invalid_credentials"


class InvalidRefreshToken(APIException):
    status_code = status.HTTP_401_UNAUTHORIZED
    default_detail = "Refresh token is invalid, expired, or already used."
    default_code = "token_not_valid"


class EmailNotVerified(APIException):
    status_code = status.HTTP_403_FORBIDDEN
    default_detail = "Please verify your email address before logging in."
    default_code = "email_not_verified"
