"""Load benchmark questions and score a graph version, from the console.

The operator's counterpart of Knowledge -> Benchmark: the same questions, the
same scoring, the same stored run. `--load` adds questions from the file the
page imports - a JSON list of {"question": ..., "expected": ...} objects, or a
CSV or .xlsx with those columns - and skips any already present, so a question
file can be loaded again after it grows.
"""

from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from platform_core.benchmark import TOP, load_questions, read_questions, run
from platform_core.models import Application, GraphRevision


class Command(BaseCommand):
    help = "Load benchmark questions and score a graph version (retrieval only, no model)."

    def add_arguments(self, parser):
        parser.add_argument("application", help="Application id")
        parser.add_argument(
            "--load", metavar="FILE", help="JSON, CSV or .xlsx of question/expected"
        )
        parser.add_argument(
            "--graph-version", type=int, help="Graph version number (default: latest)"
        )
        parser.add_argument(
            "--rerank",
            action="store_true",
            help="Also rerank with the application's reranking model (charges apply; needs --as)",
        )
        parser.add_argument("--as", dest="actor", help="Username a reranked run is charged to")

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
        reranker = None
        if options["rerank"]:
            from platform_core import rerank
            from platform_core.models import User

            reranker = User.objects.filter(username=options["actor"] or "", is_active=True).first()
            if reranker is None:
                raise CommandError("--rerank needs --as naming an active user, who is charged.")
            if rerank.configuration(app.pk) is None:
                raise CommandError("Switch on Answer reranking in AI settings first.")
        result = run(app, revision, trigger="console", reranker=reranker)
        if result is None:
            raise CommandError("The application has no benchmark questions. Use --load.")
        for item in result.results:
            mark = f"#{item['rank']}" if item["rank"] else "--"
            self.stdout.write(f"{mark:>4}  {item['expected']:<28} {item['question']}")
        self.stdout.write(
            f"\nVersion {revision.number}: found {result.found}/{result.questions}, "
            f"top {TOP} {result.top_three}/{result.questions}, MRR {result.reciprocal_rank:.2f}"
            + (" (reranked)" if reranker else "")
        )

    def load(self, app, path):
        try:
            data = path.read_bytes()
        except OSError as error:
            raise CommandError(f"Could not read {path}: {error}") from None
        try:
            with transaction.atomic():
                added, _ = load_questions(app, read_questions(path.name, data))
        except ValidationError as error:
            raise CommandError(" ".join(error.messages)) from None
        self.stdout.write(f"Loaded {added} new question(s).")
