"""Agent steps for Code Factory runs as well as chat messages (agentic plan, Phase 1).

Both fields are ones the release before can run against: the link is nullable and
carries no database constraint, and the phase has a database default.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("platform_core", "0107_agent_steps"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="agentstep",
            options={"ordering": ["message_id", "run_id", "sequence"]},
        ),
        migrations.AddField(
            model_name="agentstep",
            name="phase",
            field=models.CharField(
                blank=True, db_default="", default="", max_length=20
            ),
        ),
        migrations.AddField(
            model_name="agentstep",
            name="run",
            field=models.ForeignKey(
                blank=True,
                db_constraint=False,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="agent_steps",
                to="platform_core.factoryrun",
            ),
        ),
        migrations.AddIndex(
            model_name="agentstep",
            index=models.Index(
                fields=["run", "phase", "sequence"], name="agent_step_run"
            ),
        ),
    ]
