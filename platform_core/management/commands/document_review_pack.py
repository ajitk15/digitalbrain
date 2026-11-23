"""Compare a document's AI review with tools and with one call (charges apply).

    manage.py document_review_pack <application id> --as <username>
    manage.py document_review_pack <application id> --as <username> --record
    manage.py document_review_pack <application id> --as <username> --only tools

Each contradiction planted by benchmarks/document-review-pack.json - in memory, never
in the stored source - is reviewed through the application's Document review
configuration, credential and monthly budget, receipted as usual. See
`platform_core/document_review_pack.py`.
"""

from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.http import Http404

from platform_core import agent_measurements, document_review_pack
from platform_core.models import Application, User


class Command(BaseCommand):
    help = "Compare a document's AI review with tools and with one call (charges apply)."

    def add_arguments(self, parser):
        parser.add_argument("application", help="Application id")
        parser.add_argument("--as", dest="actor", help="Username the calls are made and charged as")
        parser.add_argument(
            "--record", action="store_true",
            help="Save under benchmarks/document-review/ and on the agent's page",
        )
        parser.add_argument("--only", choices=document_review_pack.MODES, help="Run one way only")

    def handle(self, *args, **options):
        app = Application.objects.filter(pk=options["application"] or None).first()
        user = User.objects.filter(username=options["actor"] or "", is_active=True).first()
        if app is None or user is None:
            raise CommandError("Name an application and --as an active user, who is charged.")
        modes = (options["only"],) if options["only"] else document_review_pack.MODES
        try:
            record = document_review_pack.run(user, app, modes)
        except (ValidationError, ImproperlyConfigured) as failure:
            raise CommandError(" ".join(getattr(failure, "messages", [str(failure)]))) from None
        except (PermissionDenied, Http404):
            raise CommandError(f"{user.username} may not review documents here.") from None
        for row in record["details"]:
            mark = "ok " if row["found"] else "-- "
            said = row["failure"] or (
                f"{'found' if row['found'] else 'missed'} the planted line"
                f"{', linked to a source that contradicts it' if row['linked'] else ''}; "
                f"{row['findings']} findings kept, {row['dropped']} dropped, {row['turns']} turns"
            )
            self.stdout.write(f"{mark}{row['item']} {row['mode']}: {said}")
        for mode, found in record["metrics"].items():
            self.stdout.write(
                f"{mode}: found {found['found']}/{found['items']}, linked {found['linked']}, "
                f"{found['findings']} findings, {found['failed']} failed, {found['turns']} "
                f"turns, ${found['cost']}, {found['seconds']} s"
            )
        self.stdout.write(document_review_pack.verdict(record["metrics"]))
        if options["record"] and agent_measurements.nothing_compared(record["metrics"]):
            # A run that compared nothing is not a measurement: kept neither as a
            # file nor on the agent's page, where it would read as a verdict.
            self.stdout.write("Not recorded: nothing was compared.")
        elif options["record"]:
            path = document_review_pack.save(record)
            self.stdout.write(f"Recorded {path}")
            agent_measurements.record(app, "document_review_pack", record, user, path)
            self.stdout.write("Kept for its agent's page (Settings → AI → Agents).")
