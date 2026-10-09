"""A full-text index for keyword search, on PostgreSQL only.

SQLite has no equivalent worth the name and keeps its scan; the index is
created and dropped only where the connection is PostgreSQL. The expression
must match `workbench.FULL_TEXT` exactly, or the planner will not use it.
"""

from django.db import migrations

INDEX = "platform_core_knowledge_fts"
EXPRESSION = "to_tsvector('english', left(title || ' ' || content, 200000))"


def create(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(
            f"CREATE INDEX IF NOT EXISTS {INDEX} ON platform_core_knowledgeentry "
            f"USING GIN (({EXPRESSION}))"
        )


def drop(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(f"DROP INDEX IF EXISTS {INDEX}")


class Migration(migrations.Migration):
    dependencies = [("platform_core", "0072_reranking_purpose")]

    operations = [migrations.RunPython(create, drop)]
