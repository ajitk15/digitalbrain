"""Compare the chat agent with tools and with one call, on benchmark questions (charges apply).

    manage.py chat_pack <application id> --as <username>
    manage.py chat_pack <application id> --as <username> --record
    manage.py chat_pack <application id> --as <username> --only tools --limit 10
    manage.py chat_pack <application id> --as <username> --routed

Each of the application's benchmark questions is sent through its Chat conversation
configuration, credential and monthly budget, receipted as usual. See
`platform_core/chat_pack.py`.
"""

from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.http import Http404

from platform_core import agent_measurements, chat_pack
from platform_core.models import Application, User


class Command(BaseCommand):
    help = "Compare the chat agent with tools and with one call (charges apply)."

    def add_arguments(self, parser):
        parser.add_argument("application", help="Application id")
        parser.add_argument("--as", dest="actor", help="Username the calls are made and charged as")
        parser.add_argument(
            "--record", action="store_true",
            help="Save under benchmarks/chat/ and on the agent's page",
        )
        parser.add_argument("--only", choices=(*chat_pack.MODES, chat_pack.ROUTED),
                            help="Run one way only")
        parser.add_argument(
            "--routed", action="store_true",
            help="Also ask with Chat searching's model choosing the searches (model routing)",
        )
        parser.add_argument("--limit", type=int, default=chat_pack.MAX_QUESTIONS)

    def handle(self, *args, **options):
        app = Application.objects.filter(pk=options["application"] or None).first()
        user = User.objects.filter(username=options["actor"] or "", is_active=True).first()
        if app is None or user is None:
            raise CommandError("Name an application and --as an active user, who is charged.")
        if not 1 <= options["limit"] <= chat_pack.MAX_QUESTIONS:
            raise CommandError(f"--limit must be between 1 and {chat_pack.MAX_QUESTIONS}.")
        modes = (options["only"],) if options["only"] else chat_pack.MODES
        if options["routed"] and chat_pack.ROUTED not in modes:
            modes = (*modes, chat_pack.ROUTED)
        try:
            record = chat_pack.run(user, app, modes, options["limit"])
        except (ValidationError, ImproperlyConfigured) as failure:
            raise CommandError(" ".join(getattr(failure, "messages", [str(failure)]))) from None
        except (PermissionDenied, Http404):
            raise CommandError(f"{user.username} may not use Chat here.") from None
        for row in record["details"]:
            mark = "ok " if row["cited_expected"] else "-- "
            said = row["failure"] or (
                f"{'cited' if row['cited_expected'] else 'did not cite'} {row['expected']}"
                f"{' first' if row['cited_first'] else ''}; "
                f"{row['tool_calls']} searches, {row['turns']} turns"
            )
            self.stdout.write(f"{mark}{row['mode']}: {row['question'][:70]} - {said}")
        for mode, found in record["metrics"].items():
            self.stdout.write(
                f"{mode}: cited the expected source {found['cited_expected']}/"
                f"{found['questions']} (first {found['cited_first']}, shown "
                f"{found['shown_expected']}), {found['invented']} citations to nothing, "
                f"{found['failed']} failed, {found['turns']} turns, {found['tool_calls']} "
                f"searches, ${found['cost']}, {found['seconds']} s"
            )
        self.stdout.write(chat_pack.verdict(record["metrics"]))
        if routing := chat_pack.routing_verdict(record["metrics"]):
            self.stdout.write(f"Searching with {record['routing_model']}: {routing}")
        if options["record"] and agent_measurements.nothing_compared(record["metrics"]):
            # A run that compared nothing is not a measurement: kept neither as a
            # file nor on the agent's page, where it would read as a verdict.
            self.stdout.write("Not recorded: nothing was compared.")
        elif options["record"]:
            path = chat_pack.save(record)
            self.stdout.write(f"Recorded {path}")
            agent_measurements.record(app, "chat_pack", record, user, path)
            self.stdout.write("Kept for its agent's page (Settings → AI → Agents).")
