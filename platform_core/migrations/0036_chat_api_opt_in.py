"""Turn the Chat API off for every application that already exists.

`feature_enabled` reads a missing ApplicationFeature row as enabled, which is
the right default for a feature an owner may want to switch *off*. The Chat API
is the opposite: it is the only API surface that calls a model, so it must be
switched *on* deliberately. Rather than special-case that rule, every existing
application is given an explicit disabled row here, and the create form leaves
the box unticked for new ones.

Reversible on purpose: going back removes only the rows this added, and only
where they are still disabled, so an owner who has since opted in keeps their
choice.
"""

from django.db import migrations

KEY = "chat_api"


def disable(apps, schema_editor):
    Application = apps.get_model("platform_core", "Application")
    ApplicationFeature = apps.get_model("platform_core", "ApplicationFeature")
    existing = set(
        ApplicationFeature.objects.filter(key=KEY).values_list("application_id", flat=True)
    )
    ApplicationFeature.objects.bulk_create(
        ApplicationFeature(application_id=app_id, key=KEY, enabled=False)
        for app_id in Application.objects.values_list("id", flat=True)
        if app_id not in existing
    )


def restore(apps, schema_editor):
    ApplicationFeature = apps.get_model("platform_core", "ApplicationFeature")
    ApplicationFeature.objects.filter(key=KEY, enabled=False).delete()


class Migration(migrations.Migration):
    dependencies = [("platform_core", "0035_code_graph_api_seams")]
    operations = [migrations.RunPython(disable, restore)]
