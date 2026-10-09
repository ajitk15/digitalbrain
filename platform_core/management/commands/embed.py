"""Embed a graph version for search, as a named person, from the console.

The operator's counterpart of the embedding step in a requested AI graph run:
the same texts (relationships, passages, benchmark questions), the same
content-addressed storage, the same receipts. It spends money, so it names who
pays (`--as`), that person must be allowed to change this application's
knowledge, and the embeddings purpose must be switched on in AI settings.
Texts already embedded are skipped and cost nothing.
"""

from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.http import Http404

from platform_core import embeddings
from platform_core.graphs import MAX_SOURCES, sources_for
from platform_core.models import Application, GraphRevision, User


class Command(BaseCommand):
    help = "Embed a graph version's relationships and passages for search (charges apply)."

    def add_arguments(self, parser):
        parser.add_argument("application", help="Application id")
        parser.add_argument("--as", dest="actor", required=True, help="Username charged")
        parser.add_argument(
            "--graph-version", type=int, help="Graph version number (default: latest)"
        )

    def handle(self, *args, **options):
        app = Application.objects.filter(pk=options["application"]).first()
        if app is None:
            raise CommandError("No such application.")
        user = User.objects.filter(username=options["actor"], is_active=True).first()
        if user is None:
            raise CommandError("--as must name an active user.")
        revisions = GraphRevision.objects.filter(application=app)
        revision = (
            revisions.filter(number=options["graph_version"]).first()
            if options["graph_version"]
            else revisions.first()
        )
        if revision is None:
            raise CommandError("That application has no such graph version.")
        entries = list(sources_for(app.pk)[:MAX_SOURCES])
        try:
            report = embeddings.index(user, app, revision.data, entries)
        except (Http404, PermissionDenied):
            raise CommandError(
                f"{user.username} may not change this application's knowledge."
            ) from None
        except (ValidationError, ImproperlyConfigured) as failure:
            raise CommandError(" ".join(getattr(failure, "messages", [str(failure)]))) from None
        self.stdout.write(
            f"Version {revision.number} with {report['model']}: "
            f"{report['relationships']}/{report['relationships_total']} relationships, "
            f"{report['passages']}/{report['passages_total']} passages embedded "
            f"({report['new']} new; earlier ones were reused)."
        )
