"""Switch ServiceOps AI on for applications created before it existed.

`seed_application_ai` writes one configuration per purpose when an application
is created, so an application older than the ServiceOps purpose has no row for
it and its triage runs evidence-only, with AI settings saying "Not configured".
Each one gets a row copied from its own chat configuration - the same provider,
and so the same per-application credential, model and prices - and switched on.
An application with no chat configuration is left alone rather than given a
model nobody chose. An owner can still switch it off in AI settings.
"""

from django.db import migrations


def enable(apps, schema_editor):
    AIConfiguration = apps.get_model("platform_core", "AIConfiguration")
    missing = AIConfiguration.objects.filter(purpose="chat").exclude(
        application__in=AIConfiguration.objects.filter(purpose="serviceops_triage").values(
            "application"
        )
    )
    for chat in missing:
        AIConfiguration.objects.create(
            application_id=chat.application_id,
            purpose="serviceops_triage",
            provider=chat.provider,
            model=chat.model,
            configured_by_id=chat.configured_by_id,
            input_rate=chat.input_rate,
            output_rate=chat.output_rate,
            enabled=bool(chat.model),
        )


class Migration(migrations.Migration):
    dependencies = [("platform_core", "0056_ai_settings")]

    # Not reversed: a configuration someone may have edited since is theirs.
    operations = [migrations.RunPython(enable, migrations.RunPython.noop)]
