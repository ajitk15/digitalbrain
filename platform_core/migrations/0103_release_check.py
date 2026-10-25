"""A graph version's release check, and the reason a person published over it.

Both are added so the release before can keep running against this schema: the
check is nullable and the reason has a database default.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("platform_core", "0102_automatic_ai_by_default")]

    operations = [
        migrations.AddField(
            model_name="graphrevision",
            name="release",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="graphrevision",
            name="publish_reason",
            field=models.TextField(blank=True, db_default="", default=""),
        ),
    ]
