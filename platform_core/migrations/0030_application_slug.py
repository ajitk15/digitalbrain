"""Give every application a readable handle for its API and MCP addresses.

Three steps rather than one: the column has to exist and be filled before it can
be unique, or every existing row collides on the empty string.
"""

from django.db import migrations, models
from django.utils.text import slugify


def fill_slugs(apps, schema_editor):
    Application = apps.get_model("platform_core", "Application")
    taken = set()
    for application in Application.objects.order_by("created_at", "id"):
        base = slugify(application.name)[:120] or "application"
        candidate, index = base, 2
        while candidate in taken:
            suffix = f"-{index}"
            candidate = f"{base[: 120 - len(suffix)]}{suffix}"
            index += 1
        taken.add(candidate)
        application.slug = candidate
        application.save(update_fields=["slug"])


def clear_slugs(apps, schema_editor):
    apps.get_model("platform_core", "Application").objects.update(slug="")


class Migration(migrations.Migration):
    dependencies = [("platform_core", "0029_factory_delivery")]

    operations = [
        migrations.AddField(
            model_name="application",
            name="slug",
            field=models.SlugField(blank=True, default="", max_length=140),
            preserve_default=False,
        ),
        migrations.RunPython(fill_slugs, clear_slugs),
        migrations.AlterField(
            model_name="application",
            name="slug",
            field=models.SlugField(blank=True, max_length=140, unique=True),
        ),
    ]
