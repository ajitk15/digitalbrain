"""Keyword search reads a stored tsvector instead of computing one per question.

Migrations 0073 and 0087 indexed the expression, which narrows the WHERE but
leaves `ts_rank` to tokenize every matching row again on every question: on the
scorecard's 42 sources that was a tenth of a second of `to_tsvector` per query,
and it grows with the library. A generated column is computed once, when the row
is written, and the GIN index moves onto it. Django does not know the column -
PostgreSQL fills it - so writes and SQLite are unchanged.

Adding a stored column rewrites the table once. `workbench.FULL_TEXT` and
`chunks.CHUNK_TEXT` name these columns and must keep naming them.
"""

from django.db import migrations

TABLES = (
    # table, column expression, the expression index it replaces, its new index
    (
        "platform_core_knowledgeentry",
        "to_tsvector('english', left(title || ' ' || content, 200000))",
        "platform_core_knowledge_fts",
        "platform_core_knowledge_search",
    ),
    (
        "platform_core_knowledgechunk",
        "to_tsvector('english', heading || ' ' || text)",
        "platform_core_chunk_fts",
        "platform_core_chunk_search",
    ),
)


def stored(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    for table, expression, old, new in TABLES:
        schema_editor.execute(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS search_vector tsvector "
            f"GENERATED ALWAYS AS ({expression}) STORED"
        )
        schema_editor.execute(
            f"CREATE INDEX IF NOT EXISTS {new} ON {table} USING GIN (search_vector)"
        )
        schema_editor.execute(f"DROP INDEX IF EXISTS {old}")


def computed(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    for table, expression, old, new in TABLES:
        schema_editor.execute(
            f"CREATE INDEX IF NOT EXISTS {old} ON {table} USING GIN (({expression}))"
        )
        schema_editor.execute(f"DROP INDEX IF EXISTS {new}")
        schema_editor.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS search_vector")


class Migration(migrations.Migration):
    dependencies = [("platform_core", "0087_knowledge_chunks")]

    operations = [migrations.RunPython(stored, computed)]
