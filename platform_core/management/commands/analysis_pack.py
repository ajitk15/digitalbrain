"""Compare Code Factory's gap analysis with tools and with one call (charges apply).

    manage.py analysis_pack <application id> --as <username>
    manage.py analysis_pack <application id> --as <username> --record
    manage.py analysis_pack <application id> --as <username> --only tools

Each CarePath ticket with a planted bug (benchmarks/seeded-evidence.json) is
analysed through the application's Code Factory configuration, credential and
monthly budget, receipted as usual, and scored by the planted identifiers its
plan's items cite. See `platform_core/analysis_pack.py`.
"""

from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.http import Http404

from platform_core import agent_measurements, analysis_pack
from platform_core.models import Application, User


class Command(BaseCommand):
    help = "Compare Code Factory's gap analysis with tools and with one call (charges apply)."

    def add_arguments(self, parser):
        parser.add_argument("application", help="Application id")
        parser.add_argument("--as", dest="actor", help="Username the calls are made and charged as")
        parser.add_argument(
            "--record", action="store_true",
            help="Save under benchmarks/analysis/ and on the agent's page",
        )
        parser.add_argument("--only", choices=analysis_pack.MODES, help="Run one way only")

    def handle(self, *args, **options):
        app = Application.objects.filter(pk=options["application"] or None).first()
        user = User.objects.filter(username=options["actor"] or "", is_active=True).first()
        if app is None or user is None:
            raise CommandError("Name an application and --as an active user, who is charged.")
        modes = (options["only"],) if options["only"] else analysis_pack.MODES
        try:
            record = analysis_pack.run(user, app, modes)
        except (ValidationError, ImproperlyConfigured) as failure:
            raise CommandError(" ".join(getattr(failure, "messages", [str(failure)]))) from None
        except (PermissionDenied, Http404):
            raise CommandError(f"{user.username} may not run Code Factory here.") from None
        if not record["code"]:
            self.stdout.write("No code snapshot here holds the planted files: asked without code.")
        for row in record["details"]:
            mark = "ok " if row["cited"] else "-- "
            said = row["failure"] or (
                f"items cite {row['cited']}/{row['expected']} planted identifiers "
                f"({row['offered']} in what it was handed, {row['found_more']} more found); "
                f"{row['items']} items, {row['evidenced']} with evidence, {row['rejected']} "
                f"citations refused, {row['turns']} turns"
            )
            self.stdout.write(f"{mark}{row['ticket']} {row['mode']}: {said}")
        for mode, found in record["metrics"].items():
            self.stdout.write(
                f"{mode}: cited {found['cited']}/{found['expected']} ({found['cited_share']}%), "
                f"{found['evidenced']}/{found['items']} items with evidence, {found['failed']} "
                f"failed, {found['turns']} turns, ${found['cost']}, {found['seconds']} s"
            )
        self.stdout.write(analysis_pack.verdict(record["metrics"]))
        if options["record"]:
            path = analysis_pack.save(record)
            self.stdout.write(f"Recorded {path}")
            agent_measurements.record(app, "analysis_pack", record, user, path)
            self.stdout.write("Kept for its agent's page (Settings → AI → Agents).")
