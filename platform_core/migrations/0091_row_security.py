"""Row-level security on every table that carries a required application.

See platform_core/tenancy.py. A request bound to one application reads and
writes only that application's rows of these tables; nothing bound, nothing
narrowed. PostgreSQL only - SQLite has no row security.

The list is frozen here, as a migration's must be. A table added later chooses
in its own migration, and `test_tenancy` fails until it has: every model with a
required application is either under a policy or in `tenancy.UNSCOPED`.
"""

from django.db import migrations

TABLES = [
    "platform_core_aiusage",
    "platform_core_knowledgesource",
    "platform_core_document",
    "platform_core_knowledgeentry",
    "platform_core_knowledgechunk",
    "platform_core_incidentprofile",
    "platform_core_triagerun",
    "platform_core_operationsgraph",
    "platform_core_opsnode",
    "platform_core_opsedge",
    "platform_core_automationconnection",
    "platform_core_automationrun",
    "platform_core_serviceopstestrun",
    "platform_core_coderepository",
    "platform_core_codereview",
    "platform_core_codedocumentation",
    "platform_core_chatconversation",
    "platform_core_chatretention",
    "platform_core_chatmessage",
    "platform_core_chatsubmission",
    "platform_core_apitoken",
    "platform_core_changeplan",
    "platform_core_factoryrun",
    "platform_core_connector",
    "platform_core_managedcredential",
    "platform_core_aiconfiguration",
    "platform_core_knowledgegraph",
    "platform_core_graphrevision",
    "platform_core_graphelement",
    "platform_core_benchmarkquestion",
    "platform_core_benchmarkrun",
    "platform_core_embedding",
    "platform_core_aibudget",
    "platform_core_personaldatapolicy",
    "platform_core_documentairesult",
]

FUNCTION = """
CREATE OR REPLACE FUNCTION digitalbrain_tenant() RETURNS uuid
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT NULLIF(current_setting('digitalbrain.tenant', true), '')::uuid
$$
"""
POLICY = "digitalbrain_tenant"


def forwards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(FUNCTION)
    for table in TABLES:
        schema_editor.execute(f"DROP POLICY IF EXISTS {POLICY} ON {table}")
        # USING narrows what is read, and - with no WITH CHECK of its own -
        # what may be written: a row for another application is refused.
        schema_editor.execute(
            f"CREATE POLICY {POLICY} ON {table} USING "
            "(digitalbrain_tenant() IS NULL OR application_id = digitalbrain_tenant())"
        )
        schema_editor.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        # FORCE holds the tables' owner to it too, unless it is a superuser -
        # so a suite running as a non-superuser owner is tested under it.
        schema_editor.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def backwards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    for table in TABLES:
        schema_editor.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        schema_editor.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
        schema_editor.execute(f"DROP POLICY IF EXISTS {POLICY} ON {table}")
    schema_editor.execute("DROP FUNCTION IF EXISTS digitalbrain_tenant()")


class Migration(migrations.Migration):
    dependencies = [("platform_core", "0090_ai_admissions")]

    operations = [migrations.RunPython(forwards, backwards, elidable=False)]
