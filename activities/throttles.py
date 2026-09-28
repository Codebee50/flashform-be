from rest_framework.throttling import SimpleRateThrottle

from rooms.services import normalize_code


class RoomJoinRateThrottle(SimpleRateThrottle):
    """Limits joins per room code (normalized as students type it), whatever the client IP."""

    scope = "join_room"

    def get_cache_key(self, request, view):
        return self.cache_format % {
            "scope": self.scope,
            "ident": normalize_code(view.kwargs.get("code", "")),
        }
