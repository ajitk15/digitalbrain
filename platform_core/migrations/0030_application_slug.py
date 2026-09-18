"""Give every application a readable handle for its API and MCP addresses.

Three steps rather than one: the column has to exist and be filled before it can
be unique, or every existing row collides on the empty string.

The intermediate column carries db_index=False, which is not cosmetic. A
SlugField is indexed by default, and on PostgreSQL an indexed varchar also gets
a companion "..._like" index for pattern matching. Altering that column to
unique then asks for the same "..._like" index a second time and the migration
dies with "relation ... already exists". SQLite has no such index, so the whole
test suite passes over this and only a real PostgreSQL deployment ever meets it.
The intermediate index bought nothing anyway: nothing queries the column until
it is unique.
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
            field=models.SlugField(blank=True, db_index=False, default="", max_length=140),
            preserve_default=False,
        ),
        migrations.RunPython(fill_slugs, clear_slugs),
        migrations.AlterField(
            model_name="application",
            name="slug",
            field=models.SlugField(blank=True, max_length=140, unique=True),
        ),
    ]
