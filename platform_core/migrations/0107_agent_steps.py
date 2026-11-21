"""Agent steps (`agent_runtime.harness`), with the tenant policy every table with a
required application carries (migration 0091's `secure`)."""

import importlib
import uuid

import django.db.models.deletion
from django.db import migrations, models

ROW_SECURITY = importlib.import_module("platform_core.migrations.0091_row_security")
#: Read by `test_tenancy` with 0091's `TABLES`.
SECURED = ["platform_core_agentstep"]


def forwards(apps, schema_editor):
    ROW_SECURITY.secure(schema_editor, SECURED)


def backwards(apps, schema_editor):
    ROW_SECURITY.unsecure(schema_editor, SECURED)


class Migration(migrations.Migration):

    dependencies = [
        ("platform_core", "0106_application_rules"),
    ]

    operations = [
        migrations.CreateModel(
            name="AgentStep",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("purpose", models.CharField(max_length=24)),
                ("sequence", models.PositiveIntegerField()),
                ("tool", models.CharField(max_length=64)),
                ("arguments", models.JSONField(blank=True, default=dict)),
                (
                    "outcome",
                    models.CharField(
                        choices=[
                            ("found", "Found"),
                            ("nothing", "Found nothing"),
                            ("budget", "Over the tool budget"),
                            ("stopped", "After Stop"),
                            ("refused", "Access ended"),
                            ("failed", "Failed"),
                        ],
                        max_length=10,
                    ),
                ),
                ("result_characters", models.PositiveIntegerField(default=0)),
                ("citations", models.PositiveIntegerField(default=0)),
                ("duration_ms", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "application",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="platform_core.application",
                    ),
                ),
                (
                    "message",
                    models.ForeignKey(
                        blank=True,
                        db_constraint=False,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="agent_steps",
                        to="platform_core.chatmessage",
                    ),
                ),
            ],
            options={
                "ordering": ["message_id", "sequence"],
                "indexes": [
                    models.Index(
                        fields=["message", "sequence"], name="agent_step_message"
                    )
                ],
            },
        ),
        migrations.RunPython(forwards, backwards, elidable=False),
    ]
