"""Agent measurements (`agent_measurements`), with the tenant policy every table with
a required application carries (migration 0091's `secure`)."""

import importlib
import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

ROW_SECURITY = importlib.import_module("platform_core.migrations.0091_row_security")
#: Read by `test_tenancy` with 0091's `TABLES`.
SECURED = ["platform_core_agentmeasurement"]


def forwards(apps, schema_editor):
    ROW_SECURITY.secure(schema_editor, SECURED)


def backwards(apps, schema_editor):
    ROW_SECURITY.unsecure(schema_editor, SECURED)


class Migration(migrations.Migration):

    dependencies = [
        ("platform_core", "0111_learning_drafts"),
    ]

    operations = [
        migrations.CreateModel(
            name="AgentMeasurement",
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
                ("pack", models.CharField(max_length=24)),
                ("agent", models.CharField(max_length=24)),
                ("provider", models.CharField(blank=True, default="", max_length=80)),
                ("model", models.CharField(blank=True, default="", max_length=160)),
                (
                    "pack_digest",
                    models.CharField(blank=True, default="", max_length=64),
                ),
                ("metrics", models.JSONField(blank=True, default=dict)),
                (
                    "verdict",
                    models.CharField(
                        choices=[
                            ("clears", "Clears the bar"),
                            ("no_better", "No better"),
                            ("fails", "Does not clear"),
                            ("incomplete", "Not compared"),
                            ("too_few", "Too few to judge"),
                        ],
                        max_length=12,
                    ),
                ),
                (
                    "verdict_text",
                    models.CharField(blank=True, default="", max_length=300),
                ),
                ("file", models.CharField(blank=True, default="", max_length=200)),
                ("generated_at", models.DateTimeField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "application",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="platform_core.application",
                    ),
                ),
                (
                    "recorded_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["generated_at"],
                "indexes": [
                    models.Index(
                        fields=["application", "agent", "generated_at"],
                        name="agent_measurement_by_agent",
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("application", "pack", "generated_at", "model"),
                        name="agent_measurement_once",
                    )
                ],
            },
        ),
        migrations.RunPython(forwards, backwards, elidable=False),
    ]
