from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from platform_core.models import Document, User
from platform_core.processing import convert_document


class Command(BaseCommand):
    help = "Convert up to 20 uploaded documents to Markdown with the actor's application grants."

    def add_arguments(self, parser):
        parser.add_argument("--actor", required=True)
        parser.add_argument("--application", required=True)

    def handle(self, *args, **options):
        try:
            user = User.objects.get(username=options["actor"], is_active=True)
        except User.DoesNotExist:
            raise CommandError("Active actor not found.") from None
        docs = Document.objects.filter(
            application_id=options["application"], status__in=["quarantined", "queued", "failed"]
        ).order_by("created_at")[:20]
        for doc in docs:
            try:
                convert_document(user, doc.application_id, doc.pk)
                self.stdout.write(f"{doc.pk}: processed")
            except ValidationError as error:
                self.stderr.write(f"{doc.pk}: {' '.join(error.messages)}")
