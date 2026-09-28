import hashlib

from rest_framework.throttling import SimpleRateThrottle

from .services import normalize_email


class EmailAddressRateThrottle(SimpleRateThrottle):
    """Limits requests per target email address (from the JSON body), whatever the client IP.

    Uses the `auth_email_address` rate, counted separately for each view's `throttle_scope`.
    It applies to every address, so a 429 reveals nothing about whether an account exists.
    """

    scope = "auth_email_address"

    def get_cache_key(self, request, view):
        email = request.data.get("email") if hasattr(request.data, "get") else None
        if not isinstance(email, str) or not email.strip():
            return None  # let the serializer answer 400
        digest = hashlib.sha256(normalize_email(email).encode()).hexdigest()
        return self.cache_format % {"scope": f"{self.scope}:{view.throttle_scope}", "ident": digest}
