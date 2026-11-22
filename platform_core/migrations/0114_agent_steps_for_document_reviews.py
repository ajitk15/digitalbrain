"""Agent steps for a document's AI review.

A nullable link with no database constraint, so the release before can still
delete sources while both run.
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("platform_core", "0113_agent_steps_for_docs"),
    ]

    operations = [
        migrations.AddField(
            model_name="agentstep",
            name="entry",
            field=models.ForeignKey(
                blank=True,
                db_constraint=False,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="agent_steps",
                to="platform_core.knowledgeentry",
            ),
        ),
        migrations.AddIndex(
            model_name="agentstep",
            index=models.Index(fields=["entry", "sequence"], name="agent_step_entry"),
        ),
    ]
