"""Agent measurements kept for an application's agent pages (free: calls no model).

    manage.py agent_measurements <application id>
    manage.py agent_measurements <application id> --load benchmarks/factory/<file>.json
    manage.py agent_measurements <application id> --forget benchmarks/chat/<file>.json

Lists what each agent's page shows under "How it measured". `--load` reads pack
records made elsewhere - `factory_pack`, `review_pack` or `triage_pack` run with
`--record` on a development machine - into this database, so the deployment's pages
show them too; loading the same file again changes nothing. `--forget` removes a
record kept by mistake - a run a rate limit failed - its row here and its file. See
`platform_core/agent_measurements.py`.
"""

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from platform_core import agent_measurements
from platform_core.models import AgentMeasurement, Application


class Command(BaseCommand):
    help = "List or load the agent measurements an application's agent pages show."

    def add_arguments(self, parser):
        parser.add_argument("application", help="Application id")
        parser.add_argument("--load", nargs="+", metavar="FILE", help="Pack records to keep")
        parser.add_argument("--forget", nargs="+", metavar="FILE",
                            help="Pack records kept by mistake: remove the row and the file")

    def handle(self, *args, **options):
        app = Application.objects.filter(pk=options["application"] or None).first()
        if app is None:
            raise CommandError("Name an application.")
        for name in options["load"] or ():
            try:
                row, created = agent_measurements.load(app, name)
            except ValidationError as failure:
                raise CommandError(" ".join(failure.messages)) from None
            said = "Loaded" if created else "Already kept"
            self.stdout.write(f"{said} {row.file}: {row.get_verdict_display()}")
        for name in options["forget"] or ():
            try:
                removed, existed = agent_measurements.forget(app, name)
            except ValidationError as failure:
                raise CommandError(" ".join(failure.messages)) from None
            file_said = "and its file" if existed else "(no file was there)"
            self.stdout.write(f"Forgot {name}: {removed} row(s) {file_said}")
        rows = AgentMeasurement.objects.filter(application=app).order_by("agent", "generated_at")
        if not rows:
            self.stdout.write("No measurements kept for this application.")
        for row in rows:
            self.stdout.write(
                f"{row.agent} {row.generated_at:%Y-%m-%d %H:%M} {row.model or '-'}: "
                f"{row.get_verdict_display()} - {row.verdict_text}"
            )
