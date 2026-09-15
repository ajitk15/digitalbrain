import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def disable_for_existing_apps(apps, schema_editor):
    Application = apps.get_model("platform_core", "Application")
    ApplicationFeature = apps.get_model("platform_core", "ApplicationFeature")
    ApplicationFeature.objects.bulk_create(
        [
            ApplicationFeature(application_id=pk, key="code_graph", enabled=False)
            for pk in Application.objects.values_list("pk", flat=True)
        ],
        ignore_conflicts=True,
    )


class Migration(migrations.Migration):
    dependencies = [("platform_core", "0030_application_slug")]
    operations = [
        migrations.CreateModel(
            name="CodeRepository",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("provider", models.CharField(default="github", max_length=20)),
                ("external_id", models.CharField(max_length=240)),
                ("name", models.CharField(max_length=240)),
                ("source_url", models.URLField(max_length=1000)),
                ("default_ref", models.CharField(default="main", max_length=200)),
                ("job_id", models.UUIDField(default=uuid.uuid4)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("documentation", "Source import only"),
                            ("queued", "Queued"),
                            ("indexing", "Indexing"),
                            ("ready", "Ready"),
                            ("partial", "Partial"),
                            ("failed", "Failed"),
                        ],
                        default="queued",
                        max_length=20,
                    ),
                ),
                ("error", models.CharField(blank=True, max_length=500)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "added_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL
                    ),
                ),
                (
                    "application",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="code_repositories",
                        to="platform_core.application",
                    ),
                ),
            ],
            options={"ordering": ["name", "id"]},
        ),
        migrations.CreateModel(
            name="CodeSnapshot",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("number", models.PositiveIntegerField()),
                ("ref", models.CharField(max_length=200)),
                ("commit_sha", models.CharField(max_length=64)),
                ("manifest_digest", models.CharField(max_length=64)),
                ("analyzer_version", models.CharField(default="structural-v1", max_length=40)),
                ("complete", models.BooleanField(default=True)),
                ("warnings", models.JSONField(default=list)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "repository",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="snapshots",
                        to="platform_core.coderepository",
                    ),
                ),
            ],
            options={"ordering": ["-number"]},
        ),
        migrations.CreateModel(
            name="CodeFile",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("path", models.CharField(max_length=500)),
                ("language", models.CharField(max_length=30)),
                ("digest", models.CharField(max_length=64)),
                ("content", models.TextField(max_length=400000)),
                ("parse_ok", models.BooleanField(default=True)),
                ("symbols", models.JSONField(default=list)),
                ("imports", models.JSONField(default=list)),
                ("lines", models.PositiveIntegerField(default=0)),
                (
                    "snapshot",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="files",
                        to="platform_core.codesnapshot",
                    ),
                ),
            ],
            options={"ordering": ["path", "id"]},
        ),
        migrations.CreateModel(
            name="CodeRelationship",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("kind", models.CharField(default="import", max_length=20)),
                (
                    "confidence",
                    models.CharField(
                        choices=[("static", "Static"), ("inferred", "Inferred")], max_length=12
                    ),
                ),
                ("detail", models.CharField(blank=True, max_length=500)),
                ("evidence", models.JSONField(default=dict)),
                (
                    "snapshot",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="relationships",
                        to="platform_core.codesnapshot",
                    ),
                ),
                (
                    "source",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="dependencies",
                        to="platform_core.codefile",
                    ),
                ),
                (
                    "target",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="dependents",
                        to="platform_core.codefile",
                    ),
                ),
            ],
        ),
        migrations.AddField(
            model_name="factoryrun",
            name="code_snapshot",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to="platform_core.codesnapshot",
            ),
        ),
        migrations.AddConstraint(
            model_name="coderepository",
            constraint=models.UniqueConstraint(
                fields=("application", "provider", "external_id"),
                name="unique_application_code_repository",
            ),
        ),
        migrations.AddIndex(
            model_name="coderepository",
            index=models.Index(
                fields=["application", "status"], name="platform_co_applica_dd7ac9_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="codesnapshot",
            constraint=models.UniqueConstraint(
                fields=("repository", "number"), name="unique_code_snapshot_number"
            ),
        ),
        migrations.AddConstraint(
            model_name="codesnapshot",
            constraint=models.UniqueConstraint(
                fields=("repository", "commit_sha", "manifest_digest"),
                name="unique_code_snapshot_manifest",
            ),
        ),
        migrations.AddIndex(
            model_name="codesnapshot",
            index=models.Index(
                fields=["repository", "-created_at"], name="platform_co_reposit_28fbe5_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="codefile",
            constraint=models.UniqueConstraint(
                fields=("snapshot", "path"), name="unique_code_snapshot_path"
            ),
        ),
        migrations.AddIndex(
            model_name="codefile",
            index=models.Index(
                fields=["snapshot", "language"], name="platform_co_snapsho_9b0593_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="coderelationship",
            constraint=models.UniqueConstraint(
                fields=("snapshot", "source", "target", "kind"), name="unique_code_relationship"
            ),
        ),
        migrations.AddIndex(
            model_name="coderelationship",
            index=models.Index(
                fields=["snapshot", "source"], name="platform_co_snapsho_3555f9_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="coderelationship",
            index=models.Index(
                fields=["snapshot", "target"], name="platform_co_snapsho_6230e3_idx"
            ),
        ),
        migrations.RunPython(disable_for_existing_apps, migrations.RunPython.noop),
    ]
