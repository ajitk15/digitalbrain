"""Check, and run, every agent's measurement pack (charges apply with --run).

    manage.py measure_agents <application id> --as <username>
    manage.py measure_agents <application id> --as <username> --load-questions
    manage.py measure_agents <application id> --as <username> --run

Without --run nothing is spent: it says which packs are ready and what each of the
others needs. See `platform_core/measure_agents.py`.
"""

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from platform_core import measure_agents
from platform_core.models import Application, User
from platform_core.policy import application_for


class Command(BaseCommand):
    help = "Check, and with --run run, every agent's measurement pack (charges apply)."

    def add_arguments(self, parser):
        parser.add_argument("application", help="Application id")
        parser.add_argument("--as", dest="actor", help="Username the calls are made and charged as")
        parser.add_argument("--run", action="store_true",
                            help="Run every ready pack with --record, then llm_drift (paid)")
        parser.add_argument("--load-questions", action="store_true",
                            help="Add the demo's benchmark questions if there are none (free)")

    def handle(self, *args, **options):
        app = Application.objects.filter(pk=options["application"] or None).first()
        user = User.objects.filter(username=options["actor"] or "", is_active=True).first()
        if app is None or user is None:
            raise CommandError("Name an application and --as an active user, who is charged.")
        try:
            application_for(user, app.pk)
        except Exception:
            raise CommandError(f"{user.username} may not open that application.") from None
        if options["load_questions"]:
            added = measure_agents.load_questions(app, user)
            self.stdout.write(f"Benchmark questions: {added} added." if added
                              else "Benchmark questions: already there, none added.")
        ready = []
        for check, ok, said in measure_agents.readiness(app):
            self.stdout.write(f"{'ready  ' if ok else 'not yet'} {check.agent} "
                              f"({check.pack}): {said}")
            if ok:
                ready.append(check.pack)
        drift = measure_agents.drift_ready(app)
        self.stdout.write(f"{'ready  ' if drift else 'not yet'} LLM drift (llm_drift): "
                          + ("graph generation and graph answers are set up" if drift
                             else "set up Graph generation and Graph retrieval in AI settings"))
        if not options["run"]:
            self.stdout.write(f"{len(ready)} of {len(measure_agents.checks())} packs ready. "
                              "Nothing was spent; add --run to run them (paid).")
            return
        ran = []
        for pack in ready:
            self.stdout.write(f"\n== {pack}")
            arguments = [pack, str(app.pk), "--as", user.username, "--record"]
            if pack == "chat_pack" and self.routed(app):
                arguments.append("--routed")
            try:
                call_command(*arguments, stdout=self.stdout, stderr=self.stderr)
                ran.append((pack, "done"))
            except CommandError as failure:
                self.stdout.write(f"stopped: {failure}")
                ran.append((pack, "stopped"))
        if drift:
            self.stdout.write("\n== llm_drift")
            try:
                call_command("llm_drift", str(app.pk), "--as", user.username, "--record",
                             stdout=self.stdout, stderr=self.stderr)
                ran.append(("llm_drift", "done"))
            except CommandError as failure:
                self.stdout.write(f"stopped: {failure}")
                ran.append(("llm_drift", "stopped"))
        self.stdout.write("\nRan: " + ", ".join(f"{pack} {state}" for pack, state in ran))
        self.stdout.write("Each verdict is above; recorded ones are on Settings → AI → Agents.")

    def routed(self, app):
        from platform_core.ai import routing_config
        from platform_core.models import AIConfiguration

        chat = AIConfiguration.objects.filter(application=app, purpose="chat").first()
        return chat is not None and routing_config(app, chat) is not None
