"""Compare ServiceOps triage with tools and without, on confirmed causes (charges apply).

    manage.py triage_pack <application id> --as <username>
    manage.py triage_pack <application id> --as <username> --record
    manage.py triage_pack <application id> --as <username> --only tools --limit 10
    manage.py triage_pack <application id> --as <username> --load-incidents
    manage.py triage_pack <application id> --as <username> --labelled --record

Each case is sent through the application's ServiceOps configuration, credential
and monthly budget, receipted as usual. See `platform_core/triage_pack.py`.

`--load-incidents` adds `benchmarks/triage-pack.json`'s CarePath incidents and
changes as ordinary sources and calls no model itself - but new sources rebuild the
graph, which with automatic AI on is a paid build. `--labelled` measures triage
against that pack's labels instead of causes people confirmed.
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
        parser.add_argument("--load-incidents", action="store_true",
                            help="Add the labelled CarePath incidents and changes (no model "
                                 "call; may start a paid automatic graph build)")
        parser.add_argument("--labelled", action="store_true",
                            help="Score against the pack's labels, not confirmed causes")

    def handle(self, *args, **options):
        app = Application.objects.filter(pk=options["application"] or None).first()
        user = User.objects.filter(username=options["actor"] or "", is_active=True).first()
        if app is None or user is None:
            raise CommandError("Name an application and --as an active user, who is charged.")
        if options["load_incidents"]:
            try:
                added, unchanged = triage_pack.load_incidents(user, app)
            except (ValidationError, ImproperlyConfigured) as failure:
                raise CommandError(
                    " ".join(getattr(failure, "messages", [str(failure)]))
                ) from None
            except (PermissionDenied, Http404):
                refused = f"{user.username} may not add ServiceOps records here."
                raise CommandError(refused) from None
            self.stdout.write(f"Labelled incidents: {added} record(s) added, {unchanged} "
                              "already here unchanged.")
            return
        if not 1 <= options["limit"] <= triage_pack.MAX_CASES:
            raise CommandError(f"--limit must be between 1 and {triage_pack.MAX_CASES}.")
        modes = (options["only"],) if options["only"] else triage_pack.MODES
        try:
            record = triage_pack.run(user, app, modes, options["limit"], options["labelled"])
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
        if options["record"] and agent_measurements.nothing_compared(record["metrics"]):
            # A run that compared nothing is not a measurement: kept neither as a
            # file nor on the agent's page, where it would read as a verdict.
            self.stdout.write("Not recorded: nothing was compared.")
        elif options["record"]:
            agent_measurements.stamp(app, "triage_pack", record)
            path = triage_pack.save(record)
            self.stdout.write(f"Recorded {path}")
            agent_measurements.record(app, "triage_pack", record, user, path)
            self.stdout.write("Kept for its agent's page (Settings → AI → Agents).")
