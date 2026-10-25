"""Automatic AI enrichment is on unless an owner switched it off.

Off used to be an empty `auto_refresh_by`, which now means "the first owner".
An application whose owner last pressed Switch off keeps that choice: the
latest of its `graph.auto_refresh_on`/`_off` audit events decides.
"""

from django.db import migrations, models


def keep_switched_off(apps, schema_editor):
    KnowledgeGraph = apps.get_model("platform_core", "KnowledgeGraph")
    AuditEvent = apps.get_model("platform_core", "AuditEvent")
    latest = {}
    for resource, action in (
        AuditEvent.objects.filter(action__in=["graph.auto_refresh_on", "graph.auto_refresh_off"])
        .order_by("created_at")
        .values_list("resource_id", "action")
    ):
        latest[resource] = action
    off = [resource for resource, action in latest.items() if action == "graph.auto_refresh_off"]
    for graph in KnowledgeGraph.objects.filter(auto_refresh_by__isnull=True):
        if str(graph.application_id) in off:
            KnowledgeGraph.objects.filter(pk=graph.pk).update(auto_refresh_declined=True)


class Migration(migrations.Migration):
    dependencies = [("platform_core", "0101_access_expiry_and_review")]

    operations = [
        migrations.AddField(
            model_name="knowledgegraph",
            name="auto_refresh_declined",
            field=models.BooleanField(db_default=False, default=False),
        ),
        migrations.RunPython(keep_switched_off, migrations.RunPython.noop),
    ]
