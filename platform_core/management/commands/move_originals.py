"""Move originals kept on this server's disk into the configured bucket.

Switching `[originals] store = "s3"` changes where new originals go; the ones
already on disk stay there and would read as missing. This moves each: its
bytes are checked against the SHA-256 recorded at upload, sent, confirmed in
the bucket, and only then removed from disk - a file that fails its check is
left where it is and counted. Counts only, never names. `--check` moves
nothing and fails while any original is still on disk.

Run unbound, like every management command (CLAUDE.md, "Row security").
"""

from django.core.management.base import BaseCommand, CommandError

from platform_core import originals
from platform_core.models import Document


class Command(BaseCommand):
    help = "Move originals kept on disk into the configured bucket."

    def add_arguments(self, parser):
        parser.add_argument("--application", default=None, help="Only this application id.")
        parser.add_argument(
            "--check", action="store_true", help="Move nothing; fail if any is still on disk."
        )

    def handle(self, *args, application=None, check=False, **options):
        store = originals.store()
        if not isinstance(store, originals.Bucket):
            raise CommandError('Originals are kept on disk here; set [originals] store = "s3".')
        documents = (
            Document.objects.exclude(status="deleted")
            .filter(original_removed_at__isnull=True)
            .select_related("application")
            .order_by("created_at", "pk")
        )
        if application:
            documents = documents.filter(application_id=application)
        on_disk = [d for d in documents if originals.DISK.exists(d.application, d.pk)]
        if check:
            if on_disk:
                raise CommandError(f"{len(on_disk)} originals are still on disk.")
            self.stdout.write("move_originals: no original is left on disk")
            return
        moved = refused = 0
        for document in on_disk:
            app = document.application
            path = originals.DISK.path(app, document.pk)
            if originals.sha256_of(path) != document.sha256:
                refused += 1
                continue
            store.put(app, document.pk, path)
            with store.opened(app, document.pk) as copy:
                if not copy.is_file() or originals.sha256_of(copy) != document.sha256:
                    raise CommandError("The bucket did not return what was sent. Nothing removed.")
            originals.DISK.delete(app, document.pk)
            moved += 1
        self.stdout.write(f"move_originals: moved {moved}, left {refused} that failed their check")
        if refused:
            raise CommandError(f"{refused} originals did not match their recorded digest.")
