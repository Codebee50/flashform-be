from django.conf import settings
from django.db import models
from django.db.models.functions import Upper


class Room(models.Model):
    """A persistent space owned by a teacher that students join by `code` (PRD §4, §7)."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="rooms"
    )
    name = models.CharField(max_length=100)
    # Always stored uppercase (see the check constraint), so the unique index also makes
    # codes unique case-insensitively.
    code = models.CharField(max_length=10, unique=True)
    is_locked = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(condition=models.Q(code=Upper("code")), name="room_code_uppercase"),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.code})"

    @property
    def live_activity(self):
        """The room's LIVE activity, or None.

        Uses `prefetched_live_activities` when the queryset prefetched it (see
        `rooms.services.with_live_activity`), so listing rooms doesn't query per room.
        """
        prefetched = getattr(self, "prefetched_live_activities", None)
        if prefetched is not None:
            return prefetched[0] if prefetched else None
        return self.activities.filter(status="LIVE").first()
