import re
from urllib.parse import urlparse

from django.db import migrations

REPOSITORY = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9_.-]+$")


def add_repository(CodeRepository, app_id, user_id, value, ref="main"):
    value = value.removesuffix(".git")
    if not REPOSITORY.fullmatch(value) or value.split("/")[1] in {".", ".."}:
        return
    CodeRepository.objects.get_or_create(
        application_id=app_id,
        provider="github",
        external_id=value.lower(),
        defaults={
            "added_by_id": user_id,
            "name": value,
            "source_url": f"https://github.com/{value}",
            "default_ref": ref[:200] or "main",
            "status": "documentation",
        },
    )


def backfill(apps, schema_editor):
    CodeRepository = apps.get_model("platform_core", "CodeRepository")
    Connector = apps.get_model("platform_core", "Connector")
    Document = apps.get_model("platform_core", "Document")
    for document in Document.objects.filter(origin="github").exclude(status="deleted"):
        parsed = urlparse(document.source_url)
        parts = [part for part in parsed.path.split("/") if part]
        if parsed.hostname in {"raw.githubusercontent.com", "github.com", "www.github.com"}:
            if len(parts) >= 2:
                ref = parts[2] if parsed.hostname == "raw.githubusercontent.com" and len(parts) > 2 else "main"
                add_repository(
                    CodeRepository,
                    document.application_id,
                    document.uploaded_by_id,
                    f"{parts[0]}/{parts[1]}",
                    ref,
                )
    for connector in Connector.objects.filter(kind="github"):
        repository = connector.config.get("repository") if isinstance(connector.config, dict) else ""
        if repository and connector.created_by_id:
            add_repository(
                CodeRepository,
                connector.application_id,
                connector.created_by_id,
                repository,
            )


class Migration(migrations.Migration):
    dependencies = [("platform_core", "0031_code_graph")]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
