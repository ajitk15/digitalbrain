"""Measure the platform scorecard, record it, or check it against the baseline.

    manage.py scorecard                 print today's scorecard
    manage.py scorecard --record        also save it under benchmarks/history/
    manage.py scorecard --check         fail if anything regressed past its tolerance
    manage.py scorecard --set-baseline  make today's scorecard the baseline

Builds its own workspace in a throwaway database from files in this
repository: no model is called and the local database is not touched. See
`platform_core/scorecard.py`.
"""

import json
from datetime import UTC, datetime

from django.core.management.base import BaseCommand, CommandError

from platform_core import scorecard


class Command(BaseCommand):
    help = "Measure retrieval, citation integrity, personal data, HIPAA, cost and speed."

    def add_arguments(self, parser):
        parser.add_argument("--check", action="store_true", help="Fail on a regression")
        parser.add_argument("--record", action="store_true", help="Save to benchmarks/history/")
        parser.add_argument("--set-baseline", action="store_true", help="Save as the baseline")
        parser.add_argument("--json", metavar="FILE", help="Also write the full scorecard here")
        parser.add_argument(
            "--replay",
            metavar="COMMIT",
            help="Retrieval and speed as that commit's code ranked them (with --record)",
        )

    def handle(self, *args, **options):
        card = scorecard.replay(options["replay"]) if options["replay"] else scorecard.measure()
        self.report(card)
        text = json.dumps(card, indent=1, ensure_ascii=False)
        if options["json"]:
            with open(options["json"], "w", encoding="utf-8") as output:
                output.write(text)
        if options["record"]:
            stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H%M")
            path = scorecard.BENCH / "history" / f"{stamp}-{card['commit'] or 'local'}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            self.stdout.write(f"Recorded {path.relative_to(scorecard.ROOT)}")
        if options["set_baseline"] and card.get("replayed"):
            raise CommandError("A replay of an earlier commit cannot be the baseline.")
        if options["set_baseline"]:
            (scorecard.BENCH / "baseline.json").write_text(text, encoding="utf-8")
            self.stdout.write("Baseline updated.")
        if options["check"]:
            baseline = scorecard.BENCH / "baseline.json"
            if not baseline.is_file():
                raise CommandError("No baseline yet: run with --set-baseline first.")
            found = scorecard.regressions(card, json.loads(baseline.read_text(encoding="utf-8")))
            if found:
                lines = [f"  {name}: {was} -> {now}" for name, was, now in found]
                raise CommandError("Scorecard regressed:\n" + "\n".join(lines))
            self.stdout.write(self.style.SUCCESS("No regressions against the baseline."))

    def report(self, card):
        aspect = None
        for metric in card["metrics"].values():
            if metric["aspect"] != aspect:
                aspect = metric["aspect"]
                self.stdout.write(f"\n{aspect}")
            unit = f" {metric['unit']}" if metric["unit"] else ""
            value = (
                metric.get("note", "not measured")
                if metric["value"] is None
                else f"{metric['value']}{unit}"
            )
            self.stdout.write(f"  {metric['label']:<52} {value}")
        failing = [p for p in card["details"].get("hipaa", []) if not p["passed"]]
        for probe in failing:
            self.stdout.write(f"  FAILED {probe['control']}: {probe['probe']}")
        self.stdout.write(
            f"\n{card['corpus']['sources']} sources, {card['corpus']['questions']} questions, "
            f"commit {card['commit'] or 'unknown'}"
        )
