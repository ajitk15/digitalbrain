"""Check existing documents, as new ones are checked when converted.

Records what `source_quality.run` finds, so the Sources table and each source
page can show it without reading the text. It changes nothing else: a low
score is a suggestion, never a reason a source stops being used.
"""

from collections import Counter

from django.core.management.base import BaseCommand

from platform_core.models import KnowledgeEntry
from platform_core.source_quality import result, run


class Command(BaseCommand):
    help = "Record the source check for existing sources (changes nothing else)."

    def add_arguments(self, parser):
        parser.add_argument("--application", help="Only this application id")

    def handle(self, *args, **options):
        entries = KnowledgeEntry.objects.filter(active=True, document__isnull=False)
        if options["application"]:
            entries = entries.filter(application_id=options["application"])
        verdicts = Counter()
        for entry in entries.only("id", "content", "quality").iterator():
            stored = run(entry.content)
            verdicts[result(stored)["verdict"]] += 1
            if stored != entry.quality:
                KnowledgeEntry.objects.filter(pk=entry.pk).update(quality=stored)
        checked = sum(verdicts.values())
        self.stdout.write(
            f"Checked {checked} source(s): "
            + ", ".join(f"{verdicts[name]} {name.lower()}"
                        for name in ("Ready", "Needs work", "Poor"))
            + "."
        )
