"""Prepare the audit table to become partitioned without a pause (partitions.py).

0093 attaches today's table as one partition, which needs two things it must
not build under its lock: the key every partition shares, `(id, created_at)`,
built concurrently so writes carry on; and a check that every row is older
than the cut-over, added unvalidated and then validated, a scan that does not
block writes either. The cut-over is the first of the month after next, so
the rows written between this and 0093 - moments apart in one migrate run -
cannot fail it.

Not atomic, because a concurrent index cannot be built in a transaction; each
step is safe to repeat, so a run given up on a busy lock is simply run again.
PostgreSQL only.
"""

from datetime import UTC, datetime

from django.db import migrations
from django.utils import timezone

TABLE = "platform_core_auditevent"
INDEX = "platform_core_auditevent_id_created"
RANGE = "platform_core_auditevent_history_range"


def cutover(now):
    after = now.month + 1
    return datetime(now.year + after // 12, after % 12 + 1, 1, tzinfo=UTC)


def forward(apps, schema_editor):
    connection = schema_editor.connection
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        # A concurrent build that failed leaves an invalid index behind.
        cursor.execute("SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(%s)", [INDEX])
        row = cursor.fetchone()
        if row is not None and not row[0]:
            cursor.execute(f'DROP INDEX CONCURRENTLY "{INDEX}"')
        cursor.execute(
            f'CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "{INDEX}" ON "{TABLE}" (id, created_at)'
        )
        cursor.execute(
            "SELECT 1 FROM pg_constraint WHERE conname = %s AND conrelid = to_regclass(%s)",
            [RANGE, TABLE],
        )
        if cursor.fetchone() is None:
            at = cutover(timezone.now()).isoformat()
            cursor.execute(
                f'ALTER TABLE "{TABLE}" ADD CONSTRAINT "{RANGE}" '
                f"CHECK (created_at < '{at}') NOT VALID"
            )
        cursor.execute(f'ALTER TABLE "{TABLE}" VALIDATE CONSTRAINT "{RANGE}"')


def backward(apps, schema_editor):
    connection = schema_editor.connection
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute(f'ALTER TABLE "{TABLE}" DROP CONSTRAINT IF EXISTS "{RANGE}"')
        cursor.execute(f'DROP INDEX IF EXISTS "{INDEX}"')


class Migration(migrations.Migration):
    atomic = False

    dependencies = [("platform_core", "0091_row_security")]

    operations = [migrations.RunPython(forward, backward, elidable=False)]
