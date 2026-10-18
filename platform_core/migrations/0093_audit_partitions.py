"""The audit table becomes partitioned by month, with no row copied (partitions.py).

Under one lock, for milliseconds: today's table is renamed `_history`, its key
becomes the `(id, created_at)` index 0092 built, and its indexes and
constraints take an `_h` suffix; a partitioned table takes the old name with
the same columns and every index and constraint under the name Django gave
it; the history is attached as the partition for everything before 0092's
cut-over - its validated check proves that, so nothing is scanned, and its
indexes and foreign keys are the parent's own, so nothing is built - and a
default partition takes what no month was made for. The release before keeps
writing to the same name. Months are `partitions.ensure`'s to make, which
`apply_migrations` runs next.

Reversed, every row goes back into one table under its old names.
PostgreSQL only.
"""

import re
from datetime import UTC, datetime

from django.db import migrations
from django.utils import timezone

TABLE = "platform_core_auditevent"
HISTORY = f"{TABLE}_history"
DEFAULT = f"{TABLE}_default"
INDEX = "platform_core_auditevent_id_created"
RANGE = "platform_core_auditevent_history_range"


def old(name):
    return f"{name[:61]}_h"


def forward(apps, schema_editor):
    connection = schema_editor.connection
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = %s AND conrelid = to_regclass(%s)",
            [RANGE, TABLE],
        )
        at = re.search(r"'([^']+)'", cursor.fetchone()[0])[1]
        cursor.execute(
            "SELECT conname FROM pg_constraint WHERE conrelid = to_regclass(%s) AND contype = 'p'",
            [TABLE],
        )
        primary = cursor.fetchone()[0]
        # Read before the rename, so each definition still names the table.
        cursor.execute(
            "SELECT indexrelid::regclass::text, pg_get_indexdef(indexrelid) FROM pg_index "
            "WHERE indrelid = to_regclass(%s) AND NOT indisprimary "
            "AND indexrelid <> to_regclass(%s) ORDER BY 1",
            [TABLE, INDEX],
        )
        indexes = cursor.fetchall()
        cursor.execute(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = to_regclass(%s) AND contype IN ('c', 'f') AND conname <> %s "
            "ORDER BY 1",
            [TABLE, RANGE],
        )
        constraints = cursor.fetchall()

        cursor.execute(f'ALTER TABLE "{TABLE}" RENAME TO "{HISTORY}"')
        cursor.execute(
            f'ALTER TABLE "{HISTORY}" DROP CONSTRAINT "{primary}", '
            f'ADD CONSTRAINT "{HISTORY}_pkey" PRIMARY KEY USING INDEX "{INDEX}"'
        )
        for index, _ in indexes:
            cursor.execute(f'ALTER INDEX "{index}" RENAME TO "{old(index)}"')
        for constraint, _ in constraints:
            cursor.execute(
                f'ALTER TABLE "{HISTORY}" RENAME CONSTRAINT "{constraint}" TO "{old(constraint)}"'
            )
        cursor.execute(
            f'CREATE TABLE "{TABLE}" (LIKE "{HISTORY}" INCLUDING DEFAULTS INCLUDING STORAGE '
            "INCLUDING COMMENTS) PARTITION BY RANGE (created_at)"
        )
        cursor.execute(f'ALTER TABLE "{TABLE}" ADD CONSTRAINT "{primary}" PRIMARY KEY (id, created_at)')
        for constraint, definition in constraints:
            cursor.execute(f'ALTER TABLE "{TABLE}" ADD CONSTRAINT "{constraint}" {definition}')
        for _, definition in indexes:
            cursor.execute(definition)
        cursor.execute(
            f'ALTER TABLE "{TABLE}" ATTACH PARTITION "{HISTORY}" '
            f"FOR VALUES FROM (MINVALUE) TO ('{at}')"
        )
        cursor.execute(f'CREATE TABLE "{DEFAULT}" PARTITION OF "{TABLE}" DEFAULT')


def backward(apps, schema_editor):
    connection = schema_editor.connection
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT conname FROM pg_constraint WHERE conrelid = to_regclass(%s) AND contype = 'p'",
            [TABLE],
        )
        primary = cursor.fetchone()[0]
        cursor.execute(f'ALTER TABLE "{TABLE}" DETACH PARTITION "{HISTORY}"')
        cursor.execute(f'ALTER TABLE "{HISTORY}" DROP CONSTRAINT "{RANGE}"')
        cursor.execute(f'INSERT INTO "{HISTORY}" SELECT * FROM "{TABLE}"')
        cursor.execute(f'DROP TABLE "{TABLE}"')
        cursor.execute(
            "SELECT indexrelid::regclass::text FROM pg_index "
            "WHERE indrelid = to_regclass(%s) AND NOT indisprimary",
            [HISTORY],
        )
        for (index,) in cursor.fetchall():
            if index.endswith("_h"):
                cursor.execute(f'ALTER INDEX "{index}" RENAME TO "{index[:-2]}"')
        cursor.execute(
            "SELECT conname FROM pg_constraint WHERE conrelid = to_regclass(%s) "
            "AND contype IN ('c', 'f')",
            [HISTORY],
        )
        for (constraint,) in cursor.fetchall():
            if constraint.endswith("_h"):
                cursor.execute(
                    f'ALTER TABLE "{HISTORY}" RENAME CONSTRAINT "{constraint}" '
                    f'TO "{constraint[:-2]}"'
                )
        cursor.execute(
            f'ALTER TABLE "{HISTORY}" DROP CONSTRAINT "{HISTORY}_pkey", '
            f'ADD CONSTRAINT "{primary}" PRIMARY KEY (id)'
        )
        cursor.execute(f'ALTER TABLE "{HISTORY}" RENAME TO "{TABLE}"')
        # As 0092 left it, so 0092's own reversal finds what it made.
        cursor.execute(f'CREATE UNIQUE INDEX "{INDEX}" ON "{TABLE}" (id, created_at)')
        cursor.execute(f'SELECT max(created_at) FROM "{TABLE}"')
        newest = max(filter(None, [cursor.fetchone()[0], timezone.now()]))
        after = newest.month + 1
        at = datetime(newest.year + after // 12, after % 12 + 1, 1, tzinfo=UTC).isoformat()
        cursor.execute(f"""ALTER TABLE "{TABLE}" ADD CONSTRAINT "{RANGE}" CHECK (created_at < '{at}')""")


class Migration(migrations.Migration):
    dependencies = [("platform_core", "0092_audit_partition_prepare")]

    operations = [migrations.RunPython(forward, backward, elidable=False)]
