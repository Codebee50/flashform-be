"""Channels middleware: authenticate WebSockets with a simplejwt access token in `?auth=`
(PRD §9). The token is only checked at connect time, so an open socket survives expiry."""

from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.middleware import BaseMiddleware
from django.contrib.auth.models import AnonymousUser
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import AccessToken


def query_param(scope, name: str) -> str | None:
    values = parse_qs(scope.get("query_string", b"").decode()).get(name)
    return values[0] if values else None


@database_sync_to_async
def user_for_access_token(raw: str | None):
    """The active user for a valid, unexpired access token, else AnonymousUser."""
    if not raw:
        return AnonymousUser()
    try:
        # Same user lookup and checks (user exists, is active) as the REST API.
        return JWTAuthentication().get_user(AccessToken(raw))
    except (TokenError, AuthenticationFailed):
        return AnonymousUser()


class JWTAuthMiddleware(BaseMiddleware):
    """Sets `scope["user"]` from `?auth=<access token>`; AnonymousUser when it is missing
    or invalid. Consumers decide whether that is acceptable."""

    async def __call__(self, scope, receive, send):
        scope = dict(scope, user=await user_for_access_token(query_param(scope, "auth")))
        return await super().__call__(scope, receive, send)
