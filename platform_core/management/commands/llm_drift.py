"""Check the configured models for drift on a fixed set of tasks (charges apply).

    manage.py llm_drift <application id> --as <username>
    manage.py llm_drift <application id> --as <username> --record
    manage.py llm_drift --build-pack

Sends the frozen evidence pack (`benchmarks/drift-pack.json`) to the
application's Graph generation and Graph retrieval models, scores the
replies the way the platform does, and compares them with the first and the
latest recorded run of the same models. About ten calls, through the
application's own credential and monthly budget, receipted as usual. See
`platform_core/drift.py`.
"""

from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.http import Http404

from platform_core import drift
from platform_core.models import Application, User


class Command(BaseCommand):
    help = "Measure LLM drift on fixed extraction and answer tasks (charges apply)."

    def add_arguments(self, parser):
        parser.add_argument("application", nargs="?", help="Application id")
        parser.add_argument("--as", dest="actor", help="Username the calls are made and charged as")
        parser.add_argument("--record", action="store_true", help="Save under benchmarks/drift/")
        parser.add_argument("--build-pack", action="store_true", help="Rebuild the frozen inputs")

    def handle(self, *args, **options):
        if options["build_pack"]:
            pack = drift.build_pack()
            self.stdout.write(
                f"Built {drift.PACK.name}: {len(pack['extraction'])} extraction sources, "
                f"{len(pack['answers'])} questions."
            )
            if not options["application"]:
                return
        app = Application.objects.filter(pk=options["application"] or None).first()
        user = User.objects.filter(username=options["actor"] or "", is_active=True).first()
        if app is None or user is None:
            raise CommandError("Name an application and --as an active user, who is charged.")
        try:
            record = drift.run(user, app)
        except (ValidationError, ImproperlyConfigured) as failure:
            raise CommandError(" ".join(getattr(failure, "messages", [str(failure)]))) from None
        except (PermissionDenied, Http404):
            raise CommandError(f"{user.username} may not use this application's models.") from None
        earlier = drift.history(record)
        self.stdout.write(
            f"Extraction {record['extraction_model']}, answers {record['answer_model']}, "
            f"{record['calls']} calls"
        )
        for name, (label, unit, _, _) in drift.METRICS.items():
            value = record["metrics"][name]
            first = earlier[0]["metrics"].get(name) if earlier else None
            since = f"   first run {first}" if first is not None else ""
            self.stdout.write(f"  {label:<40} {value}{' ' + unit if unit else ''}{since}")
        if record["details"]["rejection_reasons"]:
            reasons = ", ".join(
                f"{k} {v}" for k, v in record["details"]["rejection_reasons"].items()
            )
            self.stdout.write(f"  Refused: {reasons}")
        if options["record"]:
            path = drift.save(record)
            self.stdout.write(f"Recorded {path.relative_to(drift.BENCH.parent)}")
        if not earlier:
            self.stdout.write("No earlier run of these models to compare with: this one sets it.")
            return
        for label, reference in (("first", earlier[0]), ("latest", earlier[-1])):
            found = drift.drifted(record, reference)
            if found:
                lines = "\n".join(f"  {name}: {was} -> {now}" for name, was, now in found)
                self.stdout.write(self.style.WARNING(f"Drift against the {label} run:\n{lines}"))
            else:
                self.stdout.write(self.style.SUCCESS(f"No drift against the {label} run."))
