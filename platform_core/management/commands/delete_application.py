"""Delete one application and everything in it. Operator-only, and irreversible.

The platform console's Structure page does the same for a platform
administrator, once the application is disabled (`hierarchy.py`). This is the
host's version: it does not ask for the disable first, and an organization
administrator may name themselves with `--as`. Demo reset clears an
organization's work and keeps its applications; this removes an application
outright, naming it exactly.

What it removes and what it keeps - the audit trail and the AI receipts, under a
tombstone of the application row - is `hierarchy.delete`'s, so the two cannot
disagree.
"""

import uuid

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from platform_core import hierarchy
from platform_core.models import Application, OrganizationMember, User


class Command(BaseCommand):
    help = "Permanently delete one application and everything it holds."

    def add_arguments(self, parser):
        parser.add_argument("application_id")
        parser.add_argument(
            "--confirm-name",
            required=True,
            help="The application's name, exactly. Nothing is removed if it differs.",
        )
        parser.add_argument(
            "--as",
            dest="actor",
            required=True,
            help="Username recorded in the audit log: a platform or organization admin.",
        )

    def handle(self, *args, **options):
        try:
            app_id = uuid.UUID(options["application_id"])
        except ValueError:
            raise CommandError("Provide an application UUID.") from None
        app = (
            Application.objects.select_related("product__portfolio__organization")
            .filter(pk=app_id)
            .first()
        )
        if app is None:
            raise CommandError("No such application.")
        if options["confirm_name"] != app.name:
            raise CommandError(
                f"Type the application's name exactly - {app.name} - to confirm. "
                "Nothing was removed."
            )
        organization = app.product.portfolio.organization
        actor = User.objects.filter(username=options["actor"], is_active=True).first()
        if actor is None or not (
            actor.is_platform_admin
            or OrganizationMember.objects.filter(
                organization=organization, user=actor, is_admin=True
            ).exists()
        ):
            raise CommandError("--as must name an active platform or organization admin.")
        name = app.name
        try:
            result = hierarchy.delete(actor, app, options["confirm_name"], require_disabled=False)
        except ValidationError as error:
            raise CommandError(" ".join(error.messages)) from None
        self.stdout.write(f"Deleted {name} ({app_id}).")
        for model, count in sorted(result["removed"].items()):
            self.stdout.write(f"  {model}: {count}")
        if result["credentials"]:
            self.stdout.write(f"  credentials removed: {', '.join(result['credentials'])}")
        if result["files_left"]:
            self.stdout.write(
                f"  {result['files_left']} original file(s) left: the document store did not answer"
            )
