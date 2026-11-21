"""Agent steps for ServiceOps triage runs (agentic plan, Phase 3).

A nullable link with no database constraint, so the release before can still
delete triage runs while both run.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("platform_core", "0108_agent_steps_for_runs"),
    ]

    operations = [
        migrations.AddField(
            model_name="agentstep",
            name="triage",
            field=models.ForeignKey(
                blank=True,
                db_constraint=False,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="agent_steps",
                to="platform_core.triagerun",
            ),
        ),
        migrations.AddIndex(
            model_name="agentstep",
            index=models.Index(fields=["triage", "sequence"], name="agent_step_triage"),
        ),
    ]
