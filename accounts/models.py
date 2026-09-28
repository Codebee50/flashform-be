from django.conf import settings
from django.db import models


class TeacherProfile(models.Model):
    """Per-teacher data that Django's User lacks. Created for every user (see signals.py)."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile"
    )
    email_verified = models.BooleanField(default=False)
    email_verified_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"Profile of {self.user}"
