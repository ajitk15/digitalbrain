"""Delete one application and everything in it. Operator-only, and irreversible.

There is deliberately no screen for this. Demo reset clears an organization's
work and keeps its applications; this removes an application outright, which is
a decision an operator makes on the host, naming it exactly.

Order matters: most of what an application holds PROTECTs it, so everything is
removed deepest first, the same discipline `demo_reset.ORDER` follows - which is
reused for the runs, plans, graphs and triage it already knows about.
"""

import uuid

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from platform_core.demo_reset import ORDER
from platform_core.documents import document_folder, document_path
from platform_core.models import (
    AIConfiguration,
    AIUsage,
    Application,
    ChatConversation,
    ChatMessage,
    Connector,
    Document,
    FactoryRun,
    KnowledgeEntry,
    OrganizationMember,
    TriageRun,
    User,
)
from platform_core.secrets import managed_directory
from platform_core.services import audit

#: What `ORDER` does not cover, still deepest first. Entries go after every run
#: that PROTECTs one; documents after the entries converted from them.
REMAINING = (
    ChatMessage,
    ChatConversation,
    AIUsage,
    Connector,
    AIConfiguration,
    KnowledgeEntry,
)


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
        busy = (
            FactoryRun.objects.filter(application=app, status__in=["queued", "running"]).exists()
            or TriageRun.objects.filter(application=app, status__in=["queued", "running"]).exists()
        )
        if busy:
            raise CommandError("A run is queued or running here. Wait for it to finish.")

        removed = {}
        # Paths first: once deleted, the instance no longer has its id.
        files = [
            document_path(app, pk)
            for pk in Document.objects.filter(application=app).values_list("pk", flat=True)
        ]
        folder = document_folder(app)
        with transaction.atomic():
            for model, lookup in ORDER:
                count, _ = model.objects.filter(**{lookup: [app.pk]}).delete()
                if count:
                    removed[model.__name__] = count
            for model in REMAINING:
                count, _ = model.objects.filter(application=app).delete()
                if count:
                    removed[model.__name__] = count
            count, _ = Document.objects.filter(application=app).delete()
            if count:
                removed["Document"] = count
            name = app.name
            app.delete()
            audit(
                actor,
                "application.deleted",
                app_id,
                organization,
                details={"name": name, "removed": removed},
            )
        # Files after the commit: a rolled-back delete must not have lost them.
        for path in files:
            path.unlink(missing_ok=True)
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
        # Only credentials this platform wrote. An operator's mount is theirs.
        cleared = []
        directory = managed_directory()
        if directory is not None and directory.is_dir():
            for path in directory.glob(f"*_{app_id}"):
                path.unlink()
                cleared.append(path.name.removesuffix(f"_{app_id}"))
        self.stdout.write(f"Deleted {name} ({app_id}).")
        for model, count in sorted(removed.items()):
            self.stdout.write(f"  {model}: {count}")
        if cleared:
            self.stdout.write(f"  credentials removed: {', '.join(sorted(cleared))}")
