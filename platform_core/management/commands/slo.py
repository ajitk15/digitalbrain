"""Measure the service level objectives this platform keeps records for.

    manage.py slo                 each records objective over the last 28 days
    manage.py slo --json          the same as JSON
    manage.py slo --check         fail when a judged objective is below target
    manage.py slo --rules         the Prometheus rule file for every objective

Counts and ratios only - never a title, a name or an id.
"""

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from platform_core import slo

RULES = Path("deploy") / "slo-rules.yml"


class Command(BaseCommand):
    help = "Measure the service level objectives from the platform's own records."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=slo.WINDOW_DAYS)
        parser.add_argument("--json", action="store_true", dest="as_json")
        parser.add_argument("--check", action="store_true")
        parser.add_argument("--rules", action="store_true",
                            help=f"Print the Prometheus rules ({RULES} holds a copy).")

    def handle(self, *args, days, as_json, check, rules, **options):
        if rules:
            self.stdout.write(slo.prometheus_rules(), ending="")
            return
        if days < 1:
            raise CommandError("--days must be at least 1.")
        rows = slo.measure(days=days)
        if as_json:
            self.stdout.write(json.dumps(rows, indent=2))
        else:
            for row in rows:
                ratio = "-" if row["ratio"] is None else f"{row['ratio']:.2%}"
                verdict = ("met" if row["met"] else "MISSED") if row["judged"] else "too few"
                self.stdout.write(
                    f"{row['objective']:<18} {ratio:>8} of {row['total']:>6}  "
                    f"target {row['target']:.1%}  {verdict}"
                )
            self.stdout.write(
                f"Over {days} days. Metrics objectives ("
                + ", ".join(o.key for o in slo.OBJECTIVES if o.kind == "metrics")
                + f") are judged by the collector, from {RULES.as_posix()}."
            )
        missed = [row["objective"] for row in rows if not row["met"]]
        if check and missed:
            raise CommandError("Below target: " + ", ".join(missed))
