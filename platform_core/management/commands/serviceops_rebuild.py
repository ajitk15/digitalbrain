"""Rebuild incident projections from imported source revisions."""

import uuid

from django.core.management.base import BaseCommand, CommandError

from platform_core.models import Application
from platform_core.ops_graph import rebuild
from platform_core.serviceops import incident_queryset, verified
from platform_core.serviceops_triage import profile


class Command(BaseCommand):
    help = "Rebuild ServiceOps incident profiles for one application. No provider call."

    def add_arguments(self, parser):
        parser.add_argument("application_id")

    def handle(self, *args, **options):
        try:
            app_id = uuid.UUID(options["application_id"])
        except ValueError:
            raise CommandError("Provide an application UUID.") from None
        app = Application.objects.filter(pk=app_id, active=True).first()
        if not app:
            raise CommandError("No such active application.")
        count = 0
        skipped = 0
        for entry in incident_queryset(app).iterator(chunk_size=200):
            if not verified(entry):
                skipped += 1
                continue
            profile(entry)
            count += 1
        self.stdout.write(f"Rebuilt {count} incident profiles; skipped {skipped} invalid sources.")
        state = rebuild(app)
        self.stdout.write(
            f"Rebuilt the operations graph: {state.node_count} nodes, {state.edge_count} edges."
        )
