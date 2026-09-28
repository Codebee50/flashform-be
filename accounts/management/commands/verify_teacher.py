from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from accounts import services

User = get_user_model()


class Command(BaseCommand):
    help = "Mark a teacher's email as verified, e.g. for local testing without email."

    def add_arguments(self, parser):
        parser.add_argument("email")

    def handle(self, *args, email, **options):
        user = User.objects.filter(username=services.normalize_email(email)).first()
        if user is None:
            raise CommandError(f"No teacher with email {email!r}.")
        services.mark_email_verified(user)
        self.stdout.write(self.style.SUCCESS(f"Verified {user.email}."))
