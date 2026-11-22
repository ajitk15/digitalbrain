"""Compare ServiceOps triage with tools and without, on confirmed causes (charges apply).

    manage.py triage_pack <application id> --as <username>
    manage.py triage_pack <application id> --as <username> --record
    manage.py triage_pack <application id> --as <username> --only tools --limit 10

Each case is sent through the application's ServiceOps configuration, credential
and monthly budget, receipted as usual. See `platform_core/triage_pack.py`.
"""

from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.http import Http404

from platform_core import agent_measurements, triage_pack
from platform_core.models import Application, User


class Command(BaseCommand):
    help = "Compare ServiceOps triage with tools and without (charges apply)."

    def add_arguments(self, parser):
        parser.add_argument("application", help="Application id")
        parser.add_argument("--as", dest="actor", help="Username the calls are made and charged as")
        parser.add_argument("--record", action="store_true", help="Save under benchmarks/triage/")
        parser.add_argument("--only", choices=triage_pack.MODES, help="Run one way only")
        parser.add_argument("--limit", type=int, default=triage_pack.MAX_CASES)

    def handle(self, *args, **options):
        app = Application.objects.filter(pk=options["application"] or None).first()
        user = User.objects.filter(username=options["actor"] or "", is_active=True).first()
        if app is None or user is None:
            raise CommandError("Name an application and --as an active user, who is charged.")
        if not 1 <= options["limit"] <= triage_pack.MAX_CASES:
            raise CommandError(f"--limit must be between 1 and {triage_pack.MAX_CASES}.")
        modes = (options["only"],) if options["only"] else triage_pack.MODES
        try:
            record = triage_pack.run(user, app, modes, options["limit"])
        except (ValidationError, ImproperlyConfigured) as failure:
            raise CommandError(" ".join(getattr(failure, "messages", [str(failure)]))) from None
        except (PermissionDenied, Http404):
            raise CommandError(f"{user.username} may not triage here.") from None
        for row in record["details"]:
            mark = "ok " if row["rank"] else "-- "
            said = row["failure"] or (
                f"cause at rank {row['rank']}" if row["rank"] else "cause not cited"
            ) + (", in the evidence" if row["cause_in_pack"] or row["cause_read"] else "")
            self.stdout.write(f"{mark}{row['title']} ({row['mode']}): {said}")
        for mode, found in record["metrics"].items():
            self.stdout.write(
                f"{mode}: cause cited {found['cause_cited']}/{found['cases']}, first "
                f"{found['cause_first']}, MRR {found['mrr']}, in the evidence "
                f"{found['cause_in_evidence']}, {found['turns']} turns, "
                f"{found['tool_calls']} reads, ${found['cost']}, {found['seconds']} s"
            )
        self.stdout.write(triage_pack.verdict(record["metrics"]))
        if options["record"]:
            path = triage_pack.save(record)
            self.stdout.write(f"Recorded {path}")
            agent_measurements.record(app, "triage_pack", record, user, path)
            self.stdout.write("Kept for its agent's page (Settings → AI → Agents).")
