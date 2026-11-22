"""Agent steps for code documentation's AI step.

A nullable link with no database constraint, so the release before can still
delete documentation while both run.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("platform_core", "0112_agent_measurements"),
    ]

    operations = [
        migrations.AddField(
            model_name="agentstep",
            name="docs",
            field=models.ForeignKey(
                blank=True,
                db_constraint=False,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="agent_steps",
                to="platform_core.codedocumentation",
            ),
        ),
        migrations.AddIndex(
            model_name="agentstep",
            index=models.Index(fields=["docs", "sequence"], name="agent_step_docs"),
        ),
    ]
