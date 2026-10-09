"""Scan existing sources for personal data, as new ones are scanned when added.

Only records what `sensitivity.scan` finds, so the source page can say so. It
never restricts anything: labelling is a person's decision.
"""

from django.core.management.base import BaseCommand

from platform_core.models import KnowledgeEntry
from platform_core.sensitivity import scan


class Command(BaseCommand):
    help = "Record what looks like personal data in existing sources (labels nothing)."

    def add_arguments(self, parser):
        parser.add_argument("--application", help="Only this application id")

    def handle(self, *args, **options):
        entries = KnowledgeEntry.objects.filter(active=True)
        if options["application"]:
            entries = entries.filter(application_id=options["application"])
        flagged = scanned = 0
        for entry in entries.only("id", "title", "content", "personal_data").iterator():
            found = scan(f"{entry.title}\n{entry.content}")
            scanned += 1
            flagged += bool(found)
            if found != entry.personal_data:
                KnowledgeEntry.objects.filter(pk=entry.pk).update(personal_data=found)
        self.stdout.write(
            f"Scanned {scanned} source(s); {flagged} look like they hold personal data."
        )
