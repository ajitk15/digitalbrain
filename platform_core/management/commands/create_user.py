from getpass import getpass

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from platform_core.models import User


class Command(BaseCommand):
    help = "Provision a regular user. Roles are assigned separately in the application."

    def add_arguments(self, parser):
        parser.add_argument("username")

    def handle(self, *args, **options):
        user = User(username=options["username"])
        if User.objects.filter(username=user.username).exists():
            raise CommandError("User ID already exists.")
        try:
            user.full_clean(exclude=["password", "last_login"])
            password = getpass("New user password (14+ characters): ")
            if password != getpass("Confirm password: "):
                raise CommandError("Passwords do not match.")
            validate_password(password, user)
        except ValidationError as error:
            raise CommandError("; ".join(error.messages)) from None
        user.set_password(password)
        user.save()
        self.stdout.write(
            self.style.SUCCESS("User created without administrative or application access.")
        )
