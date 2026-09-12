from getpass import getpass

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from platform_core.models import AuditEvent, User


class Command(BaseCommand):
    help = "Create the initial platform administrator using an interactive password prompt."

    @transaction.atomic
    def handle(self, *args, **options):
        username = settings.SITE_ADMIN_USER_ID
        if not username:
            raise CommandError("Set SITE_ADMIN_USER_ID in .env (only this key is allowed).")
        if User.objects.filter(is_platform_admin=True).exists():
            raise CommandError("Platform administrator already exists; bootstrap is disabled.")
        if User.objects.filter(username=username).exists():
            raise CommandError("User ID already exists. Bootstrap cannot elevate existing users.")
        user = User(username=username, is_platform_admin=True)
        try:
            user.full_clean(exclude=["password", "last_login"])
            password = getpass("New administrator password (14+ characters): ")
            if password != getpass("Confirm password: "):
                raise CommandError("Passwords do not match.")
            validate_password(password, user)
        except ValidationError as error:
            raise CommandError("; ".join(error.messages)) from None
        user.set_password(password)
        user.save()
        AuditEvent.objects.create(
            actor=user, action="platform.admin_bootstrapped", resource_id=str(user.pk)
        )
        self.stdout.write(self.style.SUCCESS("Platform administrator created."))
