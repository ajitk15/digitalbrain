"""Compare code documentation's AI step with tools and with one call (charges apply).

    manage.py docs_pack <application id> --as <username>
    manage.py docs_pack <application id> --as <username> --repository acme/care --record
    manage.py docs_pack <application id> --as <username> --only tools

The newest documentation's components are described through the application's Code
documentation configuration, credential and monthly budget, receipted as usual.
See `platform_core/docs_pack.py`.
"""

from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.http import Http404

from platform_core import agent_measurements, docs_pack
from platform_core.models import Application, User


class Command(BaseCommand):
    help = "Compare code documentation's AI step with tools and with one call (charges apply)."

    def add_arguments(self, parser):
        parser.add_argument("application", help="Application id")
        parser.add_argument("--as", dest="actor", help="Username the calls are made and charged as")
        parser.add_argument("--repository", default="", help="owner/name; the newest by default")
        parser.add_argument(
            "--record", action="store_true",
            help="Save under benchmarks/docs/ and on the agent's page",
        )
        parser.add_argument("--only", choices=docs_pack.MODES, help="Run one way only")

    def handle(self, *args, **options):
        app = Application.objects.filter(pk=options["application"] or None).first()
        user = User.objects.filter(username=options["actor"] or "", is_active=True).first()
        if app is None or user is None:
            raise CommandError("Name an application and --as an active user, who is charged.")
        modes = (options["only"],) if options["only"] else docs_pack.MODES
        try:
            record = docs_pack.run(user, app, modes, options["repository"])
        except (ValidationError, ImproperlyConfigured) as failure:
            raise CommandError(" ".join(getattr(failure, "messages", [str(failure)]))) from None
        except (PermissionDenied, Http404):
            raise CommandError(
                f"{user.username} may not write code documentation here."
            ) from None
        for row in record["details"]:
            said = row["failure"] or (
                f"described {row['components']}/{row['asked']} components, kept "
                f"{row['paragraphs']} paragraphs quoting {row['files_quoted']} files "
                f"({row['kept_share']}% kept their quotes), {row['tool_calls']} reads, "
                f"{row['turns']} turns, ${row['cost']}, {row['seconds']} s"
            )
            self.stdout.write(f"{row['mode']}: {said}")
        self.stdout.write(docs_pack.verdict(record["metrics"]))
        if options["record"] and agent_measurements.nothing_compared(record["metrics"]):
            # A run that compared nothing is not a measurement: kept neither as a
            # file nor on the agent's page, where it would read as a verdict.
            self.stdout.write("Not recorded: nothing was compared.")
        elif options["record"]:
            path = docs_pack.save(record)
            self.stdout.write(f"Recorded {path}")
            agent_measurements.record(app, "docs_pack", record, user, path)
            self.stdout.write("Kept for its agent's page (Settings → AI → Agents).")
