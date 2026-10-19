"""Service level objectives: counted from the platform's own records, or ruled on metrics."""

import re
from datetime import timedelta
from io import StringIO
from pathlib import Path

from django.conf import settings
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from platform_core import metrics, slo
from platform_core.models import ChatConversation, ChatMessage, Job

from . import test_documents
from .test_telemetry import COLLECTOR


@override_settings(DOCUMENT_AUTO_CONVERT=False)
class RecordsTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.now = timezone.now()
        self.conversation = ChatConversation.objects.create(
            application=self.app, user=self.owner, title="Q")
        self.sequence = 0

    def answer(self, status, seconds=5, ago=timedelta(hours=1)):
        self.sequence += 1
        message = ChatMessage.objects.create(
            application=self.app, user=self.owner, conversation=self.conversation,
            role="assistant", status=status, sequence=self.sequence)
        asked = self.now - ago
        ChatMessage.objects.filter(pk=message.pk).update(
            created_at=asked,
            finished_at=None if status == "streaming" else asked + timedelta(seconds=seconds),
        )

    def job(self, status, due_ago, started_after=None, finished=True):
        due = self.now - due_ago
        Job.objects.create(
            kind="graph_refresh", lane="graph", key=f"graph:{Job.objects.count()}",
            status=status, run_after=due,
            started_at=None if started_after is None else due + started_after,
            finished_at=due + (started_after or timedelta()) if finished else None,
        )

    def test_an_answer_counts_by_how_it_ended_and_a_stop_is_neither(self):
        for status in ("complete", "complete", "failed", "stopped", "streaming"):
            self.answer(status)
        self.answer("failed", ago=timedelta(days=slo.WINDOW_DAYS + 1))
        since = self.now - timedelta(days=slo.WINDOW_DAYS)
        self.assertEqual(slo.answers_finish(since, self.now), (2, 3))

    def test_an_answer_is_on_time_inside_the_threshold(self):
        self.answer("complete", seconds=slo.ANSWER_SECONDS - 1)
        self.answer("complete", seconds=slo.ANSWER_SECONDS + 1)
        self.answer("failed", seconds=1)
        since = self.now - timedelta(days=1)
        self.assertEqual(slo.answers_in_time(since, self.now), (1, 2))

    def test_work_that_started_late_or_is_still_waiting_late_is_bad(self):
        limit = timedelta(seconds=slo.START_SECONDS)
        self.job("done", timedelta(hours=1), started_after=limit - timedelta(seconds=1))
        self.job("done", timedelta(hours=1), started_after=limit + timedelta(seconds=1))
        self.job("queued", timedelta(hours=1), finished=False)
        # Due a moment ago and not started: not judged yet.
        self.job("queued", timedelta(seconds=10), finished=False)
        since = self.now - timedelta(days=1)
        self.assertEqual(slo.work_starts(since, self.now), (1, 3))

    def test_work_succeeds_unless_it_used_every_attempt(self):
        self.job("done", timedelta(hours=1), started_after=timedelta())
        self.job("dead", timedelta(hours=1), started_after=timedelta())
        self.job("running", timedelta(hours=1), started_after=timedelta(), finished=False)
        since = self.now - timedelta(days=1)
        self.assertEqual(slo.work_succeeds(since, self.now), (1, 2))

    def test_too_few_events_are_reported_and_never_fail_a_check(self):
        self.answer("failed")
        (row,) = [r for r in slo.measure() if r["objective"] == "answers_finish"]
        self.assertEqual((row["good"], row["total"], row["judged"], row["met"]),
                         (0, 1, False, True))
        call_command("slo", "--check", stdout=StringIO())

    def test_a_judged_objective_below_target_fails_the_check(self):
        for _ in range(slo.MIN_EVENTS):
            self.answer("complete")
        self.answer("failed")
        (row,) = [r for r in slo.measure() if r["objective"] == "answers_finish"]
        self.assertTrue(row["judged"])
        self.assertFalse(row["met"])
        self.assertLess(row["budget_left"], 0)
        out = StringIO()
        with self.assertRaisesMessage(CommandError, "answers_finish"):
            call_command("slo", "--check", stdout=out)
        self.assertIn("MISSED", out.getvalue())

    @override_settings(OTEL=COLLECTOR)
    def test_the_maintenance_lane_logs_counts_and_holds_the_gauge(self):
        reader = InMemoryMetricReader()
        metrics.use(MeterProvider(metric_readers=[reader], views=metrics.views()))
        self.addCleanup(metrics.use, None)
        self.answer("complete")
        self.answer("failed")
        slo._read = None
        with self.assertLogs("platform_core.slo", "INFO") as logged:
            self.assertFalse(slo.read_if_due())
        finish = [r for r in logged.records if r.objective == "answers_finish"]
        self.assertEqual((finish[0].good, finish[0].total), (1, 2))
        points = {
            (p.attributes["digitalbrain.objective"], p.attributes["digitalbrain.outcome"]): p.value
            for r in reader.get_metrics_data().resource_metrics for s in r.scope_metrics
            for m in s.metrics if m.name == "digitalbrain.slo.events"
            for p in m.data.data_points
        }
        self.assertEqual(points[("answers_finish", "good")], 1)
        self.assertEqual(points[("answers_finish", "bad")], 1)


class RulesTests(SimpleTestCase):
    def test_the_committed_rules_are_the_registry(self):
        committed = (Path(settings.BASE_DIR) / "deploy" / "slo-rules.yml").read_text(
            encoding="utf-8")
        self.assertEqual(committed, slo.prometheus_rules(),
                         "Run `manage.py slo --rules > deploy/slo-rules.yml`.")

    def test_every_objective_is_alerted_on(self):
        rules = slo.prometheus_rules()
        for objective in slo.OBJECTIVES:
            alerts = rules.count(f"          objective: {objective.key}\n        annotations:")
            self.assertEqual(alerts, 2 if objective.kind == "metrics" else 1, objective.key)

    def test_the_rules_read_only_exported_metric_names(self):
        rules = slo.prometheus_rules()
        exported = {name.replace(".", "_") + suffix
                    for name, spec in metrics.INSTRUMENTS.items()
                    for suffix in (("_seconds",) if spec.unit == "s" else ("",))}
        for objective in slo.OBJECTIVES:
            for expression in (objective.errors, objective.total):
                # A metric name is what a selector or a range follows; a label is not.
                for name in re.findall(r"((?:http|digitalbrain)_[a-z_]+)(?=[{\[])",
                                       expression):
                    base = name.removesuffix("_count").removesuffix("_bucket")
                    self.assertIn(base, exported, objective.key)
        self.assertNotIn("\\", rules)

    def test_every_selector_names_a_series_and_labels_it_carries(self):
        """Names as Prometheus writes an OTLP metric, checked against collector 0.120
        and Prometheus 3.2 (deploy/observability): dots are underscores, seconds a
        suffix, a counter ends `_total`, and an attribute is its dotted name -
        `lane` is `digitalbrain_lane`. The rules once wrote `lane=` and
        `objective=`, which matched no series, so those alerts could never fire."""
        series = {}
        for name, spec in metrics.INSTRUMENTS.items():
            base = name.replace(".", "_") + ("_seconds" if spec.unit == "s" else "")
            keys = {key.replace(".", "_") for key in spec.attributes} | {"job", "instance"}
            if spec.kind == "histogram":
                for suffix in ("_bucket", "_count", "_sum"):
                    series[base + suffix] = keys | ({"le"} if suffix == "_bucket" else set())
            else:
                series[base + ("_total" if spec.kind == "counter" else "")] = keys
        recorded = set(re.findall(r"record: (\S+)", slo.prometheus_rules()))
        for expression in re.findall(r"expr: '(.*)'", slo.prometheus_rules()):
            for name, selector in re.findall(r"([a-z_:][a-z0-9_:]*)(\{[^}]*\}|\[)", expression):
                if name in {"rate", "avg_over_time", "max", "sum", "vector", "bool"}:
                    continue
                allowed = {"objective"} if name in recorded else series.get(name)
                self.assertIsNotNone(allowed, f"{name} is not exported: {expression}")
                used = set(re.findall(r"([a-z_]+)=~?\"", selector))
                self.assertLessEqual(used, allowed, f"{name} carries no {used - allowed}")

    def test_keys_are_unique_and_targets_are_ratios(self):
        self.assertEqual(len(slo.BY_KEY), len(slo.OBJECTIVES))
        for objective in slo.OBJECTIVES:
            self.assertIn(objective.kind, {"records", "metrics"})
            self.assertTrue(0 < objective.target < 1)
            self.assertEqual(objective.count is not None, objective.kind == "records")
