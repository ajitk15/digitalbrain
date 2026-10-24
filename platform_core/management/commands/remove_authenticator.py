"""Remove a person's authenticator app, on the host.

For a lost phone and no recovery code left, where the People screen will not
do it: a platform administrator's own account, or nobody left to press the
button. Their password alone signs them in afterwards. Audited as the person
themselves, since the host operator has no account of their own here.
"""

from django.core.management.base import BaseCommand, CommandError

from platform_core import authenticator
from platform_core.models import User


class Command(BaseCommand):
    help = "Remove a user's authenticator app, so their password alone signs them in."

    def add_arguments(self, parser):
        parser.add_argument("username")

    def handle(self, *args, **options):
        user = User.objects.filter(username=options["username"]).first()
        if user is None:
            raise CommandError("No such user.")
        removed = authenticator.remove(user, user, "removed on the host")
        self.stdout.write("Removed." if removed else "No authenticator app was added.")
