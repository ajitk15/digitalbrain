"""The audit table is partitioned by month on PostgreSQL (partitions.py).

These run on PostgreSQL only and say so elsewhere: SQLite has no partitions,
and there every function is a no-op, which the last class pins.
"""

import io
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase, override_settings
from django.utils import timezone

from platform_core import partitions
from platform_core.models import AuditEvent, User

TABLE = "platform_core_auditevent"


def where(event):
    """The partition a row is stored in."""
    with connection.cursor() as cursor:
        cursor.execute(f'SELECT tableoid::regclass::text FROM "{TABLE}" WHERE id = %s', [event.pk])
        return cursor.fetchone()[0]


class PostgreSQLOnly(TestCase):
    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("Only PostgreSQL partitions the audit table; SQLite keeps one.")
        self.user = User.objects.create_user("auditor", password="x" * 16)
        self.now = timezone.now()

    def event(self, at):
        event = AuditEvent.objects.create(actor=self.user, action="test.event", resource_id="r")
        AuditEvent.objects.filter(pk=event.pk).update(created_at=at)
        return event

    def ranges(self):
        with connection.cursor() as cursor:
            return partitions.partitions(cursor, TABLE)

    def cutover(self):
        return self.ranges()[partitions.history(TABLE)][1]


class SwapTests(PostgreSQLOnly):
    def test_the_table_is_partitioned_over_its_history_and_a_default(self):
        with connection.cursor() as cursor:
            self.assertTrue(partitions.partitioned(cursor, TABLE))
            self.assertTrue(partitions.exists(cursor, partitions.default(TABLE)))
        lower, upper = self.ranges()[partitions.history(TABLE)]
        self.assertIsNone(lower)
        self.assertGreater(upper, self.now, "the cut-over is ahead of every row written so far")

    def test_the_parent_keeps_the_names_django_gave_it(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT conname, contype FROM pg_constraint WHERE conrelid = %s::regclass",
                [TABLE],
            )
            found = cursor.fetchall()
            cursor.execute("SELECT indexname FROM pg_indexes WHERE tablename = %s", [TABLE])
            indexes = {name for (name,) in cursor.fetchall()}
        kinds, names = sorted(kind for _, kind in found), {name for name, _ in found}
        self.assertEqual(kinds, ["f", "f", "p"])
        self.assertIn(f"{TABLE}_pkey", names)
        self.assertTrue(any(name.startswith("platform_core_audite_actor_id_") for name in names))
        actor = "platform_core_auditevent_actor_id_"
        self.assertTrue(any(name.startswith(actor) for name in indexes), indexes)

    def test_the_orm_reads_and_writes_through_the_parent(self):
        event = AuditEvent.objects.create(actor=self.user, action="test.event", resource_id="r")
        self.assertEqual(where(event), partitions.history(TABLE))
        self.assertEqual(AuditEvent.objects.get(pk=event.pk).action, "test.event")
        AuditEvent.objects.filter(pk=event.pk).delete()
        self.assertFalse(AuditEvent.objects.filter(pk=event.pk).exists())


class EnsureTests(PostgreSQLOnly):
    def test_months_are_made_ahead_and_a_row_lands_in_its_own(self):
        report = partitions.ensure(now=self.now)[TABLE]
        self.assertGreaterEqual(len(report["made"]), partitions.MONTHS_AHEAD - 2)
        found = partitions.status(now=self.now)[TABLE]
        self.assertGreaterEqual(found["ahead"], partitions.MONTHS_AHEAD - 1)
        self.assertFalse(found["waiting"])
        cutover = self.cutover()
        event = self.event(cutover + timedelta(days=3))
        self.assertEqual(where(event), partitions.name(TABLE, cutover))
        again = partitions.ensure(now=self.now)[TABLE]
        self.assertEqual(again["made"], [], "a second run makes nothing")

    def test_a_row_no_month_was_made_for_waits_in_the_default_and_is_moved(self):
        far = self.now + timedelta(days=3 * 365)
        event = self.event(far)
        self.assertEqual(where(event), partitions.default(TABLE))
        self.assertTrue(partitions.status(now=self.now)[TABLE]["waiting"])
        report = partitions.ensure(now=far)[TABLE]
        self.assertEqual(report["moved"], 1)
        self.assertEqual(where(event), partitions.name(TABLE, partitions.month(far)))
        self.assertFalse(partitions.status(now=far)[TABLE]["waiting"])
        self.assertEqual(AuditEvent.objects.get(pk=event.pk).resource_id, "r")

    def test_only_what_is_wholly_past_the_window_is_dropped(self):
        partitions.ensure(now=self.now)
        cutover = self.cutover()
        kept = self.event(cutover + timedelta(days=20))
        gone = self.event(self.now)
        later = cutover + timedelta(days=2200)
        # The test's own transaction still owes the foreign key checks of the
        # rows it wrote; a table with checks pending cannot be dropped. The
        # migrate job runs this in a transaction of its own.
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        report = partitions.ensure(now=later, retention={"audit_days": 2192})[TABLE]
        self.assertIn(partitions.history(TABLE), report["dropped"])
        self.assertNotIn(partitions.name(TABLE, cutover), report["dropped"])
        self.assertFalse(AuditEvent.objects.filter(pk=gone.pk).exists())
        self.assertTrue(AuditEvent.objects.filter(pk=kept.pk).exists())
        with connection.cursor() as cursor:
            self.assertTrue(partitions.exists(cursor, partitions.default(TABLE)))

    def test_without_a_window_nothing_is_dropped(self):
        event = self.event(self.now)
        later = self.now + timedelta(days=800)
        for retention in (None, {}, {"usage_days": 400}):
            report = partitions.ensure(now=later, retention=retention)[TABLE]
            self.assertEqual(report["dropped"], [])
        self.assertTrue(AuditEvent.objects.filter(pk=event.pk).exists())

    def test_a_shortfall_is_reported(self):
        partitions.ensure(now=self.now)
        later = self.now + timedelta(days=400)
        said = partitions.problems(partitions.status(now=later))
        self.assertTrue(any("months made ahead" in line for line in said), said)
        self.assertEqual(partitions.problems(partitions.status(now=self.now)), [])


class CommandTests(PostgreSQLOnly):
    def test_check_fails_on_a_row_waiting_in_the_default(self):
        partitions.ensure(now=self.now)
        call_command("partitions", check=True, stdout=io.StringIO())
        self.event(self.now + timedelta(days=3000))
        with self.assertRaisesMessage(CommandError, "rows are waiting"):
            call_command("partitions", check=True, stdout=io.StringIO())

    @override_settings(RETENTION={"audit_days": 2192})
    def test_the_command_and_the_migrate_job_both_ensure(self):
        out = io.StringIO()
        call_command("partitions", stdout=out)
        self.assertIn(f"{TABLE}: made", out.getvalue())
        with (
            patch("platform_core.management.commands.apply_migrations.call_command"),
            patch.object(partitions, "ensure", return_value={}) as ensure,
        ):
            call_command("apply_migrations", stdout=io.StringIO())
        ensure.assert_called_once_with(retention={"audit_days": 2192})

    def test_the_maintenance_lane_warns_and_never_raises(self):
        partitions._last = None
        with (
            patch.object(partitions, "status", return_value={TABLE: {"ahead": 0, "waiting": True}}),
            self.assertLogs("platform_core.partitions", "WARNING") as logged,
        ):
            self.assertFalse(partitions.warn_if_due())
        self.assertEqual(logged.records[0].event, "partitions_short")
        partitions._last = None
        with (
            patch.object(partitions, "status", side_effect=RuntimeError("gone")),
            self.assertLogs("platform_core.partitions", "WARNING") as logged,
        ):
            self.assertFalse(partitions.warn_if_due())
        self.assertEqual(logged.records[0].event, "partitions_failed")


class BoundTests(TestCase):
    def test_bounds_read_back_as_postgresql_prints_them(self):
        self.assertIsNone(partitions.value("MINVALUE"))
        self.assertEqual(
            partitions.value("'2026-12-01 00:00:00+00'"), datetime(2026, 12, 1, tzinfo=UTC)
        )
        self.assertEqual(partitions.following(datetime(2026, 12, 1, tzinfo=UTC)),
                         datetime(2027, 1, 1, tzinfo=UTC))

    def test_sqlite_has_nothing_to_do(self):
        if connection.vendor == "postgresql":
            self.skipTest("SQLite's case.")
        self.assertEqual(partitions.ensure(), {})
        self.assertEqual(partitions.status(), {})
        self.assertFalse(partitions.warn_if_due())
        out = io.StringIO()
        call_command("partitions", stdout=out)
        self.assertIn("nothing to do", out.getvalue())
