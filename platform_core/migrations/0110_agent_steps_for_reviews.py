"""Agent steps for a code review's AI step (agentic plan, Phase 4).

A nullable link with no database constraint, so the release before can still
delete reviews while both run.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("platform_core", "0109_agent_steps_for_triage"),
    ]

    operations = [
        migrations.AddField(
            model_name="agentstep",
            name="review",
            field=models.ForeignKey(
                blank=True,
                db_constraint=False,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="agent_steps",
                to="platform_core.codereview",
            ),
        ),
        migrations.AddIndex(
            model_name="agentstep",
            index=models.Index(fields=["review", "sequence"], name="agent_step_review"),
        ),
    ]
