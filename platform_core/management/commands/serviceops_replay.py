"""Time-correct, label-aware baseline for incident retrieval."""

import uuid

from django.core.management.base import BaseCommand, CommandError

from platform_core.models import Application
from platform_core.serviceops import (
    fields_and_description,
    incident_queryset,
    precedent_rows,
    verified,
)
from platform_core.serviceops_triage import _date


class Command(BaseCommand):
    help = "Replay resolved incidents using only precedents available when they opened."

    def add_arguments(self, parser):
        parser.add_argument("application_id")
        parser.add_argument("--limit", type=int, default=500)

    def handle(self, *args, **options):
        try:
            app_id = uuid.UUID(options["application_id"])
        except ValueError:
            raise CommandError("Provide an application UUID.") from None
        app = Application.objects.filter(pk=app_id, active=True).first()
        if not app:
            raise CommandError("No such active application.")
        limit = options["limit"]
        if limit < 1 or limit > 10000:
            raise CommandError("--limit must be between 1 and 10000.")
        examined = eligible = hits = 0
        for incident in incident_queryset(app).order_by("created_at")[:limit]:
            examined += 1
            if not verified(incident):
                continue
            fields, _ = fields_and_description(incident)
            opened = _date(fields.get("Opened"))
            resolved = _date(fields.get("Resolved"))
            label = fields.get("Problem") or fields.get("Caused by change")
            if not (opened and resolved and resolved > opened and label):
                continue
            eligible += 1
            results = precedent_rows(app, incident, as_of=opened)
            if any(
                (row["fields"].get("Problem") or row["fields"].get("Caused by change")) == label
                for row in results
            ):
                hits += 1
        self.stdout.write(
            f"Examined {examined}; labelled, time-correct cases {eligible}; "
            f"related cause in top 5 {hits}."
        )
        if eligible < 500:
            self.stdout.write("Insufficient cases for the 500-incident rollout gate.")
        else:
            self.stdout.write(f"Top-5 cause-link recall: {hits / eligible:.3f}")
