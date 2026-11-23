"""Compare a code review's AI step with tools and without (charges apply).

    manage.py review_pack <application id> --as <username>
    manage.py review_pack <application id> --as <username> --record
    manage.py review_pack <application id> --as <username> --only tools

Each change of benchmarks/review-pack.json is reviewed through the application's
Code review configuration, credential and monthly budget, receipted as usual.
See `platform_core/review_pack.py`.
"""

from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.http import Http404

from platform_core import agent_measurements, review_pack
from platform_core.models import Application, User


class Command(BaseCommand):
    help = "Compare a code review's AI step with tools and without (charges apply)."

    def add_arguments(self, parser):
        parser.add_argument("application", help="Application id")
        parser.add_argument("--as", dest="actor", help="Username the calls are made and charged as")
        parser.add_argument("--record", action="store_true", help="Save under benchmarks/review/")
        parser.add_argument("--only", choices=review_pack.MODES, help="Run one way only")

    def handle(self, *args, **options):
        app = Application.objects.filter(pk=options["application"] or None).first()
        user = User.objects.filter(username=options["actor"] or "", is_active=True).first()
        if app is None or user is None:
            raise CommandError("Name an application and --as an active user, who is charged.")
        modes = (options["only"],) if options["only"] else review_pack.MODES
        try:
            record = review_pack.run(user, app, modes)
        except (ValidationError, ImproperlyConfigured) as failure:
            raise CommandError(" ".join(getattr(failure, "messages", [str(failure)]))) from None
        except (PermissionDenied, Http404):
            raise CommandError(f"{user.username} may not review code here.") from None
        for row in record["details"]:
            mark = "ok " if row["found"] else "-- "
            said = row["failure"] or (
                f"{row['findings']} finding(s), {row['noise']} noise, {row['read']} read"
            )
            self.stdout.write(f"{mark}{row['change']} ({row['mode']}): {said}")
        for mode, found in record["metrics"].items():
            self.stdout.write(
                f"{mode}: found {found['found']}/{found['changes']}, noise {found['noise']}, "
                f"{found['turns']} turns, {found['tool_calls']} reads, ${found['cost']}, "
                f"{found['seconds']} s"
            )
        self.stdout.write(review_pack.verdict(record["metrics"]))
        if options["record"] and agent_measurements.nothing_compared(record["metrics"]):
            # A run that compared nothing is not a measurement: kept neither as a
            # file nor on the agent's page, where it would read as a verdict.
            self.stdout.write("Not recorded: nothing was compared.")
        elif options["record"]:
            path = review_pack.save(record)
            self.stdout.write(f"Recorded {path}")
            agent_measurements.record(app, "review_pack", record, user, path)
            self.stdout.write("Kept for its agent's page (Settings → AI → Agents).")
