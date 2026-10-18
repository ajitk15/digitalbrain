"""Write stored passages for sources that have none current (`chunks.py`).

The maintenance lane does this a batch at a time on its own; this is the same
work in one go, before switching `retrieval_candidates = "chunks"`, and
`--check` says whether anything is still behind.
"""

from django.core.management.base import BaseCommand, CommandError

from platform_core.chunks import BACKFILL_BATCH, backfill, behind


class Command(BaseCommand):
    help = "Write keyword-search passages for sources that have none current."

    def add_arguments(self, parser):
        parser.add_argument("--application", default=None, help="Only this application id.")
        parser.add_argument(
            "--check", action="store_true", help="Write nothing; fail if any source is behind."
        )

    def handle(self, *args, application=None, check=False, **options):
        pending = behind()
        if application:
            pending = pending.filter(application_id=application)
        if check:
            count = pending.count()
            if count:
                raise CommandError(f"{count} sources have no current passages.")
            self.stdout.write("chunk_sources: every active source has current passages")
            return
        total = 0
        while written := backfill(BACKFILL_BATCH, application_id=application):
            total += written
        self.stdout.write(f"chunk_sources: wrote passages for {total} sources")
