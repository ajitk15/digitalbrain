"""Load benchmark questions and score a graph version, from the console.

The operator's counterpart of Knowledge -> Benchmark: the same questions, the
same scoring, the same stored run. `--load` adds questions from a JSON list of
{"question": ..., "expected": ...} objects and skips any already present, so a
question file can be loaded again after it grows.
"""

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from platform_core.benchmark import MAX_QUESTIONS, TOP, BenchmarkQuestionForm, run
from platform_core.models import Application, BenchmarkQuestion, GraphRevision


class Command(BaseCommand):
    help = "Load benchmark questions and score a graph version (retrieval only, no model)."

    def add_arguments(self, parser):
        parser.add_argument("application", help="Application id")
        parser.add_argument("--load", metavar="FILE", help="JSON list of question/expected objects")
        parser.add_argument(
            "--graph-version", type=int, help="Graph version number (default: latest)"
        )

    def handle(self, *args, **options):
        app = Application.objects.filter(pk=options["application"]).first()
        if app is None:
            raise CommandError("No such application.")
        if options["load"]:
            self.load(app, Path(options["load"]))
        revisions = GraphRevision.objects.filter(application=app)
        revision = (
            revisions.filter(number=options["graph_version"]).first()
            if options["graph_version"]
            else revisions.first()
        )
        if revision is None:
            raise CommandError("That application has no such graph version.")
        result = run(app, revision, trigger="console")
        if result is None:
            raise CommandError("The application has no benchmark questions. Use --load.")
        for item in result.results:
            mark = f"#{item['rank']}" if item["rank"] else "--"
            self.stdout.write(f"{mark:>4}  {item['expected']:<28} {item['question']}")
        self.stdout.write(
            f"\nVersion {revision.number}: found {result.found}/{result.questions}, "
            f"top {TOP} {result.top_three}/{result.questions}, MRR {result.reciprocal_rank:.2f}"
        )

    def load(self, app, path):
        try:
            items = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise CommandError(f"Could not read {path}: {error}") from None
        if not isinstance(items, list):
            raise CommandError("The file must hold a JSON list.")
        existing = set(
            BenchmarkQuestion.objects.filter(application=app).values_list("question", flat=True)
        )
        added = 0
        for item in items:
            form = BenchmarkQuestionForm(item if isinstance(item, dict) else {})
            if not form.is_valid():
                raise CommandError(f"Invalid question {item!r}: {form.errors.as_text()}")
            if form.cleaned_data["question"] in existing:
                continue
            if len(existing) >= MAX_QUESTIONS:
                raise CommandError(f"An application can hold {MAX_QUESTIONS} questions.")
            BenchmarkQuestion.objects.create(application=app, **form.cleaned_data)
            existing.add(form.cleaned_data["question"])
            added += 1
        self.stdout.write(f"Loaded {added} new question(s).")
