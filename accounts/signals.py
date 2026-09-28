from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import TeacherProfile


@receiver(post_save, sender=settings.AUTH_USER_MODEL, dispatch_uid="accounts.create_teacher_profile")
def create_teacher_profile(sender, instance, created, raw=False, **kwargs):
    if created and not raw:
        TeacherProfile.objects.get_or_create(user=instance)
