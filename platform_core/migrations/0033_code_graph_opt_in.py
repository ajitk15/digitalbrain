"""Make Code Graph opt-in for applications that existed before it did.

A missing ApplicationFeature row means enabled, so adding "code_graph" to
services.FEATURES would otherwise switch the feature on everywhere at once and
put a new entry in every application's menu. Existing applications get an
explicit disabled row instead; an owner turns it on from the Features screen.
Applications created after this migration are unaffected, because the create
form writes their feature rows itself.
"""

from django.db import migrations


def disable_for_existing(apps, schema_editor):
    Application = apps.get_model("platform_core", "Application")
    ApplicationFeature = apps.get_model("platform_core", "ApplicationFeature")
    decided = set(
        ApplicationFeature.objects.filter(key="code_graph").values_list(
            "application_id", flat=True
        )
    )
    ApplicationFeature.objects.bulk_create(
        [
            ApplicationFeature(application_id=app_id, key="code_graph", enabled=False)
            for app_id in Application.objects.exclude(pk__in=decided).values_list(
                "pk", flat=True
            )
        ]
    )


def remove(apps, schema_editor):
    apps.get_model("platform_core", "ApplicationFeature").objects.filter(
        key="code_graph", enabled=False
    ).delete()


class Migration(migrations.Migration):
    dependencies = [("platform_core", "0032_backfill_code_repositories")]
    operations = [migrations.RunPython(disable_for_existing, remove)]
