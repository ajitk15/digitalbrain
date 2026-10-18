"""Audit events and usage receipts leave only when a deployment says so.

Phase 5 (F12). Nothing is removed by default; a window below its floor is
refused; rows go oldest first in small batches; what went is logged as counts.
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from digitalbrain.configuration import load_config
from platform_core import retention
from platform_core.models import (
    AIUsage,
    Application,
    AuditEvent,
    Organization,
    Portfolio,
    Product,
)


class FloorTests(SimpleTestCase):
    def config(self, tmp, retention_table):
        path = tmp / "config.toml"
        path.write_text(
            'mode = "development"\nhosts = ["localhost"]\n'
            f'secret_directory = "{tmp.as_posix()}"\n' + retention_table,
            encoding="utf-8",
        )
        with patch.dict("os.environ", {"DIGITAL_BRAIN_CONFIG": str(path)}):
            return load_config()

    def test_a_window_below_its_floor_is_refused_and_the_floor_is_accepted(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            for table, refused in [
                ("[retention]\naudit_days = 365\n", True),
                ("[retention]\nusage_days = 30\n", True),
                ("[retention]\nkeep_everything_days = 30\n", True),
                ('[retention]\naudit_days = "forever"\n', True),
                ("[retention]\naudit_days = 2192\nusage_days = 400\n", False),
                ("[retention]\naudit_days = 0\n", False),
            ]:
                with self.subTest(table):
                    if refused:
                        with self.assertRaises(ImproperlyConfigured):
                            self.config(tmp, table)
                    else:
                        self.config(tmp, table)

    def test_six_years_is_six_years_with_the_leap_days(self):
        self.assertGreaterEqual(retention.FLOORS["audit_days"], 6 * 365 + 2)


class PurgeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("owner", password="not-a-real-password")
        org = Organization.objects.create(name="Northstar")
        product = Product.objects.create(
            name="Payments", portfolio=Portfolio.objects.create(name="Commerce", organization=org)
        )
        cls.app = Application.objects.create(name="Refunds", product=product)

    def setUp(self):
        retention._last = 0.0
        self.now = timezone.now()

    def audit(self, days_old):
        event = AuditEvent.objects.create(
            actor=self.user, action="probe", resource_id=str(days_old)
        )
        AuditEvent.objects.filter(pk=event.pk).update(
            created_at=self.now - timedelta(days=days_old)
        )
        return event

    def receipt(self, days_old):
        usage = AIUsage.objects.create(
            application=self.app, actor=self.user, provider="test", model="m",
            request_id=f"r-{days_old}", input_tokens=1, output_tokens=1, amount=Decimal("0.01"),
        )
        AIUsage.objects.filter(pk=usage.pk).update(created_at=self.now - timedelta(days=days_old))
        return usage

    def test_nothing_is_removed_unless_a_window_is_set(self):
        self.audit(5000)
        self.receipt(5000)
        with override_settings(RETENTION={}):
            self.assertFalse(retention.purge_if_due(self.now))
        self.assertEqual((AuditEvent.objects.count(), AIUsage.objects.count()), (1, 1))

    def test_rows_past_the_window_go_and_the_rest_stay(self):
        old, kept = self.audit(2300), self.audit(2000)
        old_receipt, this_month = self.receipt(500), self.receipt(3)
        with override_settings(RETENTION={"audit_days": 2192, "usage_days": 400}):
            with self.assertLogs("platform_core.retention", "INFO") as logged:
                self.assertTrue(retention.purge_if_due(self.now))
        self.assertEqual(list(AuditEvent.objects.values_list("pk", flat=True)), [kept.pk])
        self.assertEqual(list(AIUsage.objects.values_list("pk", flat=True)), [this_month.pk])
        record = logged.records[0]
        self.assertEqual((record.audit_days, record.usage_days), (1, 1))
        self.assertNotIn(str(old.pk), logged.output[0])
        self.assertNotIn(str(old_receipt.pk), logged.output[0])

    def test_a_large_backlog_goes_a_batch_at_a_time_oldest_first(self):
        for days in range(2200, 2210):
            self.audit(days)
        with (
            patch.object(retention, "BATCH", 3),
            patch.object(retention, "BATCHES_PER_STEP", 2),
        ):
            removed = retention.purge(AuditEvent, 2192, self.now)
        self.assertEqual(removed, 6)
        left = sorted(int(r) for r in AuditEvent.objects.values_list("resource_id", flat=True))
        self.assertEqual(left, [2200, 2201, 2202, 2203])

    def test_it_runs_at_most_once_an_interval(self):
        self.audit(3000)
        with override_settings(RETENTION={"audit_days": 2192}):
            self.assertTrue(retention.purge_if_due(self.now))
            self.audit(3000)
            self.assertFalse(retention.purge_if_due(self.now))
        self.assertEqual(AuditEvent.objects.count(), 1)

    def test_a_failure_is_logged_and_never_raised(self):
        with (
            override_settings(RETENTION={"audit_days": 2192}),
            patch.object(retention, "purge", side_effect=RuntimeError("database gone")),
            self.assertLogs("platform_core.retention", "WARNING") as logged,
        ):
            self.assertFalse(retention.purge_if_due(self.now))
        self.assertEqual(logged.records[0].event, "retention_failed")

    def test_it_is_a_maintenance_step(self):
        from platform_core.document_worker import maintenance_steps

        self.assertIn(retention.purge_if_due, maintenance_steps())
