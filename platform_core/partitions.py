"""Audit events live in monthly partitions on PostgreSQL.

`AuditEvent` is the table every request writes to and nothing shrank: a
sign-in, a change, a paid call - each is a row, kept six years at the least
(`retention.FLOORS`). As one table, every retention step is a delete of rows a
batch at a time and every vacuum a pass over all of them. Partitioned by month,
a month past the window leaves as one `DROP TABLE`, and the months still kept
are never touched.

Migrations 0092 and 0093 made the table partitioned without copying a row.
0092 builds the key every partition shares, `(id, created_at)`, concurrently,
and proves every row older than a cut-over a month or two ahead, so neither
scan blocks a write. 0093 renames the table to `<table>_history`, puts a
partitioned table under the old name - so the release before, still serving,
writes to it unchanged - and attaches the history as the partition for
everything before the cut-over, which takes milliseconds. A `<table>_default`
partition takes any row no month was made for, so a write never fails because
a deployment went a year without one.

A partition is a table, and making or dropping one is DDL the runtime role
cannot run (`database_roles.py`). So `ensure` runs where the owner connects:
`apply_migrations`, on every deploy, and `manage.py partitions`, which an
operator may schedule. It makes each month from the earliest partition to
`MONTHS_AHEAD` ahead, moves into a month the rows the default took for it, and
- only where `[retention] audit_days` is set - drops a partition whose every
row is past the window: rows `retention.purge` would have deleted a batch at a
time, so nothing leaves that would not have left. The maintenance lane, which
cannot fix it, warns when fewer than `MIN_AHEAD` months are made or the
default holds rows (`warn_if_due`); `manage.py partitions --check` says the
same as a health probe.

`AIUsage` is deliberately one table still. Its receipt key - application,
provider, request id - is how a provider request is recorded once, and
PostgreSQL enforces a unique key across partitions only when it includes the
partition key: the same request could then be recorded again in a new month.
It holds one row per paid call, fewer than the audit rows each receipt also
writes, and its window is purged a batch at a time as before.

SQLite has none of this; every function here does nothing there.
"""

import logging
import re
import time
from datetime import UTC, datetime, timedelta

from django.db import connection as default_connection
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

#: Each partitioned table, by the retention window that governs it.
TABLES = {"platform_core_auditevent": "audit_days"}
KEY = "created_at"
#: Months made ahead of the current one on every run.
MONTHS_AHEAD = 12
#: Fewer than this ahead and `status` reports a problem.
MIN_AHEAD = 3
#: Seconds between the maintenance lane's checks.
INTERVAL = 3600

BOUND = re.compile(r"FROM \((.+)\) TO \((.+)\)")


def month(moment):
    return datetime(moment.year, moment.month, 1, tzinfo=UTC)


def following(start):
    return datetime(start.year + start.month // 12, start.month % 12 + 1, 1, tzinfo=UTC)


def history(table):
    return f"{table}_history"


def default(table):
    return f"{table}_default"


def name(table, start):
    return f"{table}_p{start:%Y%m}"


def literal(moment):
    """A bound written into DDL. Only ever a datetime made here, never input."""
    return "'" + moment.astimezone(UTC).isoformat() + "'"


def value(text):
    """One side of a partition bound as PostgreSQL prints it; None for MINVALUE."""
    if text in ("MINVALUE", "MAXVALUE"):
        return None
    text = text.strip("'")
    if re.search(r"[+-]\d\d$", text):
        text += ":00"
    return datetime.fromisoformat(text).astimezone(UTC)


def exists(cursor, relation):
    cursor.execute("SELECT to_regclass(%s) IS NOT NULL", [relation])
    return cursor.fetchone()[0]


def partitioned(cursor, table):
    cursor.execute("SELECT relkind FROM pg_class WHERE oid = to_regclass(%s)", [table])
    row = cursor.fetchone()
    return row is not None and row[0] == "p"


def partitions(cursor, table):
    """`{name: (lower, upper)}` of each range partition; the default is not one."""
    cursor.execute(
        "SELECT c.relname, pg_get_expr(c.relpartbound, c.oid) FROM pg_inherits i "
        "JOIN pg_class c ON c.oid = i.inhrelid WHERE i.inhparent = to_regclass(%s)",
        [table],
    )
    found = {}
    for relation, bound in cursor.fetchall():
        match = BOUND.search(bound)
        if match:
            found[relation] = (value(match[1]), value(match[2]))
    return found


def covered(ranges, start):
    return any(
        (lower is None or lower <= start) and (upper is None or start < upper)
        for lower, upper in ranges
    )


def ahead(ranges, now):
    """Whole months made after the current one, counted until the first gap."""
    count, start = 0, following(month(now))
    while covered(ranges, start) and count < 1200:
        count += 1
        start = following(start)
    return count


def make(using, table, start):
    """Make one month's partition, moving into it what the default took for it."""
    end, part, fallback = following(start), name(table, start), default(table)
    bounds = f"FROM ({literal(start)}) TO ({literal(end)})"
    where = f'"{KEY}" >= {literal(start)} AND "{KEY}" < {literal(end)}'
    moved = 0
    with transaction.atomic(using=using.alias), using.cursor() as cursor:
        if exists(cursor, fallback):
            # No row may reach the default for this month while it is moved.
            cursor.execute(f'LOCK TABLE "{fallback}" IN SHARE ROW EXCLUSIVE MODE')
            cursor.execute(f'SELECT count(*) FROM "{fallback}" WHERE {where}')
            moved = cursor.fetchone()[0]
        if not moved:
            cursor.execute(f'CREATE TABLE "{part}" PARTITION OF "{table}" FOR VALUES {bounds}')
            return 0
        cursor.execute(
            f'CREATE TABLE "{part}" (LIKE "{table}" INCLUDING DEFAULTS INCLUDING STORAGE)'
        )
        cursor.execute(f'INSERT INTO "{part}" SELECT * FROM "{fallback}" WHERE {where}')
        cursor.execute(f'DELETE FROM "{fallback}" WHERE {where}')
        cursor.execute(f'ALTER TABLE "{table}" ATTACH PARTITION "{part}" FOR VALUES {bounds}')
    return moved


def drop(using, table, part):
    with transaction.atomic(using=using.alias), using.cursor() as cursor:
        cursor.execute(f'ALTER TABLE "{table}" DETACH PARTITION "{part}"')
        cursor.execute(f'DROP TABLE "{part}"')


def ensure(now=None, retention=None, using=default_connection):
    """Make the months ahead, empty the default, drop what is past its window.

    `retention` is `settings.RETENTION`; a table whose window is not set loses
    nothing. Returns `{table: {"made": [...], "moved": n, "dropped": [...]}}`.
    """
    if using.vendor != "postgresql":
        return {}
    now = now or timezone.now()
    report = {}
    for table, window in TABLES.items():
        with using.cursor() as cursor:
            if not partitioned(cursor, table):
                continue
            existing = partitions(cursor, table)
        done = report[table] = {"made": [], "moved": 0, "dropped": []}
        days = (retention or {}).get(window)
        if days:
            cutoff = now - timedelta(days=days)
            for part, (_, upper) in sorted(existing.items(), key=lambda item: item[1][1] or now):
                if upper is not None and upper <= cutoff:
                    drop(using, table, part)
                    done["dropped"].append(part)
                    del existing[part]
        finite = [b for bounds in existing.values() for b in bounds if b is not None]
        start = month(min(finite)) if finite else month(now)
        horizon = month(now)
        for _ in range(MONTHS_AHEAD):
            horizon = following(horizon)
        while start <= horizon:
            if not covered(existing.values(), start):
                done["moved"] += make(using, table, start)
                done["made"].append(name(table, start))
                existing[name(table, start)] = (start, following(start))
            start = following(start)
    return report


def status(now=None, using=default_connection):
    """`{table: {"ahead": months, "waiting": rows in the default?}}`. Reads only."""
    if using.vendor != "postgresql":
        return {}
    now = now or timezone.now()
    found = {}
    with using.cursor() as cursor:
        for table in TABLES:
            if not partitioned(cursor, table):
                continue
            waiting = False
            if exists(cursor, default(table)):
                cursor.execute(f'SELECT EXISTS (SELECT 1 FROM "{default(table)}")')
                waiting = cursor.fetchone()[0]
            found[table] = {
                "ahead": ahead(partitions(cursor, table).values(), now),
                "waiting": waiting,
            }
    return found


def problems(found):
    said = []
    for table, figures in found.items():
        if figures["ahead"] < MIN_AHEAD:
            said.append(f"{table}: {figures['ahead']} months made ahead, fewer than {MIN_AHEAD}")
        if figures["waiting"]:
            said.append(f"{table}: rows are waiting in {default(table)} for a month to be made")
    return said


_last = 0.0


def warn_if_due(now=None):
    """A maintenance step: say when the migrate job is needed. Never raises."""
    global _last
    if default_connection.vendor != "postgresql" or time.monotonic() - _last < INTERVAL:
        return False
    _last = time.monotonic()
    try:
        found = status(now)
    except Exception as failure:
        logger.warning(
            "partition check failed",
            extra={"event": "partitions_failed", "exception_type": type(failure).__name__},
        )
        return False
    for table, figures in found.items():
        if problems({table: figures}):
            logger.warning(
                "partitions short",
                extra={
                    "event": "partitions_short",
                    "table": table,
                    "months_ahead": figures["ahead"],
                    "default_rows": figures["waiting"],
                },
            )
    return False
