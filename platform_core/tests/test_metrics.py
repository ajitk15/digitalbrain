"""Metrics: rates, errors and durations, ids and labels only, off by default.

They come on with the `[otel]` table that sends traces and go to the same
collector through the same exporter. What each instrument may carry is a
closed registry, a subset of what a span may say, with no per-call value.
"""

import gzip
import os
import tempfile
import threading
import uuid
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider

from platform_core import document_worker, metrics, telemetry
from platform_core.ai import invoke_ai
from platform_core.chat_views import finish_answer
from platform_core.models import AIConfiguration, ChatConversation, ChatMessage, Job
from platform_core.workbench import add_knowledge

from . import test_claude_messages, test_documents
from .test_telemetry import COLLECTOR, QUESTION, Collector, ExporterTests, OtelSettingTests


class Measured:
    def start_measuring(self):
        on = override_settings(OTEL=COLLECTOR)
        on.enable()
        self.addCleanup(on.disable)
        self.reader = InMemoryMetricReader()
        metrics.use(MeterProvider(metric_readers=[self.reader], views=metrics.views()))
        self.addCleanup(metrics.use, None)

    def points(self, name):
        data = self.reader.get_metrics_data()
        found = []
        for resource in data.resource_metrics if data else []:
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    if metric.name == name:
                        found += metric.data.data_points
        return found

    def assertOnlyRegistered(self):
        data = self.reader.get_metrics_data()
        for resource in data.resource_metrics if data else []:
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    allowed = metrics.INSTRUMENTS[metric.name].attributes
                    for point in metric.data.data_points:
                        self.assertLessEqual(set(point.attributes), allowed, metric.name)


class RegistryTests(SimpleTestCase):
    def test_an_instrument_says_only_what_a_span_may(self):
        for name, spec in metrics.INSTRUMENTS.items():
            self.assertLessEqual(spec.attributes, telemetry.ATTRIBUTES, name)

    def test_no_instrument_takes_a_value_that_is_new_on_every_call(self):
        per_call = {"digitalbrain.request_id", "gen_ai.response.id",
                    "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens",
                    "digitalbrain.wait_ms"}
        for name, spec in metrics.INSTRUMENTS.items():
            self.assertFalse(spec.attributes & per_call, name)

    def test_only_spend_is_by_application(self):
        by_application = [name for name, spec in metrics.INSTRUMENTS.items()
                          if "digitalbrain.application_id" in spec.attributes]
        self.assertEqual(by_application, ["digitalbrain.ai.cost"])


class OffTests(SimpleTestCase):
    @override_settings(OTEL={})
    def test_without_an_otel_table_nothing_is_measured(self):
        metrics.use(None)
        self.assertIsNone(metrics.meter())
        metrics.record("http.server.request.duration", 1.0)

    @override_settings(OTEL={**COLLECTOR, "metrics": False})
    def test_metrics_false_turns_them_off_and_leaves_traces(self):
        metrics.use(None)
        self.assertFalse(metrics.enabled())
        self.assertIsNone(metrics.meter())
        self.assertTrue(telemetry.enabled())


@override_settings(CLAUDE_MESSAGES_API=True)
class PlatformMetricTests(Measured, TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.source = add_knowledge(
            self.owner, self.app.pk, "Policy", "Refunds are accepted for thirty days."
        )
        self.start_measuring()
        # Traces on with the same table, kept by nothing.
        telemetry.use(TracerProvider())
        self.addCleanup(telemetry.use, None)

    def test_a_request_is_its_method_route_pattern_and_status(self):
        response = self.client.get(reverse("documents", args=[self.app.pk]) + "?q=MRN+4471")
        self.assertEqual(response.status_code, 200)
        (point,) = self.points("http.server.request.duration")
        self.assertEqual(dict(point.attributes), {
            "http.request.method": "GET",
            "http.route": "applications/<uuid:pk>/documents/",
            "http.response.status_code": 200,
            "digitalbrain.streamed": False,
        })
        self.assertEqual(point.count, 1)
        self.assertEqual(tuple(point.explicit_bounds), metrics.SECONDS)
        self.assertOnlyRegistered()

    def test_the_dashboard_is_its_own_route_and_a_404_is_unmatched(self):
        self.client.get("/")
        self.client.get("/no-such-page/")
        routes = {point.attributes["http.route"]
                  for point in self.points("http.server.request.duration")}
        self.assertEqual(routes, {"/", "unmatched"})

    def test_a_lane_step_that_did_work_is_timed_and_an_idle_one_is_not(self):
        stop = threading.Event()

        def found_work():
            return True

        def found_nothing():
            stop.set()
            return False

        def broke():
            raise RuntimeError("row it cannot read")

        # The lane closes stale connections between steps; inside a TestCase that
        # is the test's own connection, which PostgreSQL then really closes and
        # every later test in the class fails to reach (SQLite ignores it).
        with (
            self.assertLogs("platform_core.document_worker", "WARNING"),
            patch("platform_core.document_worker.close_old_connections"),
        ):
            document_worker.run_lane("intake", lambda: (found_work, broke, found_nothing), 0, stop)
        outcomes = {point.attributes["digitalbrain.outcome"]: point.count
                    for point in self.points("digitalbrain.lane.step.duration")}
        self.assertEqual(outcomes, {"ok": 1, "error": 1})
        self.assertOnlyRegistered()

    def test_a_paid_call_is_its_wait_duration_tokens_and_spend(self):
        AIConfiguration.objects.create(
            application=self.app, purpose="chat", provider="claude", model="claude-sonnet-5",
            input_rate=Decimal("2"), output_rate=Decimal("10"), configured_by=self.owner,
        )
        citations = [{"id": str(self.source.pk), "title": "Policy",
                      "excerpt": self.source.content}]
        transport = test_claude_messages.Transport((200, test_claude_messages.reply()))
        with (
            patch("platform_core.secrets.application_secret",
                  return_value=test_claude_messages.KEY),
            patch.object(test_claude_messages.claude_messages, "_http_client",
                         transport.client),
        ):
            invoke_ai(self.owner, self.app.pk, "chat", QUESTION, citations)
        (wait,) = self.points("digitalbrain.ai.admission.wait")
        self.assertEqual(wait.attributes["digitalbrain.outcome"], "admitted")
        (held,) = self.points("gen_ai.client.operation.duration")
        self.assertEqual(held.attributes["digitalbrain.outcome"], "answered")
        self.assertEqual(held.attributes["gen_ai.request.model"], "claude-sonnet-5")
        tokens = {p.attributes["gen_ai.token.type"]: p.value
                  for p in self.points("gen_ai.client.token.usage")}
        self.assertEqual(tokens["input"], 150)
        (spend,) = self.points("digitalbrain.ai.cost")
        self.assertEqual(spend.attributes["digitalbrain.application_id"], str(self.app.pk))
        self.assertGreater(spend.value, 0)
        self.assertOnlyRegistered()

    def test_an_answer_is_timed_once_from_asking_to_its_end(self):
        conversation = ChatConversation.objects.create(
            application=self.app, user=self.owner, title="Q")
        message = ChatMessage.objects.create(
            application=self.app, user=self.owner, conversation=conversation,
            role="assistant", status="streaming", sequence=2, mode="ai")
        self.assertEqual(finish_answer(message.pk, "complete", "Thirty days.", [],
                                       provider="claude", model="m"), 1)
        self.assertEqual(finish_answer(message.pk, "failed", "", []), 0)
        (point,) = self.points("digitalbrain.answer.duration")
        self.assertEqual(dict(point.attributes), {
            "digitalbrain.mode": "ai", "gen_ai.provider.name": "claude",
            "digitalbrain.outcome": "complete",
        })
        self.assertEqual(point.count, 1)

    def test_queue_figures_are_read_by_the_maintenance_lane_for_the_gauges(self):
        from django.utils import timezone

        Job.objects.create(kind="graph_refresh", lane="graph", key="graph:x",
                           run_after=timezone.now())
        metrics._observed = 0.0
        self.assertFalse(metrics.observe())
        waiting = {p.attributes["digitalbrain.lane"]: p.value
                   for p in self.points("digitalbrain.queue.waiting")}
        queued = Job.objects.filter(lane="graph", status="queued").count()
        self.assertEqual(waiting["graph"], queued)
        self.assertGreaterEqual(waiting["graph"], 1)
        self.assertEqual(waiting["intake"], 0)
        self.assertIn("digitalbrain.answers.streaming",
                      {m.name for r in self.reader.get_metrics_data().resource_metrics
                       for s in r.scope_metrics for m in s.metrics})
        self.assertOnlyRegistered()


class SenderTests(SimpleTestCase):
    listen = ExporterTests.listen

    def test_metrics_reach_the_same_collector_and_nothing_from_the_environment(self):
        from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
            ExportMetricsServiceRequest,
        )

        endpoint = self.listen()
        secrets = tempfile.TemporaryDirectory()
        self.addCleanup(secrets.cleanup)
        token = Path(secrets.name) / "otel_authorization"
        token.write_text("Bearer collector-token", encoding="utf-8")
        os.chmod(token, 0o600)
        planted = {"OTEL_EXPORTER_OTLP_HEADERS": "x-leak=operator",
                   "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT": "http://127.0.0.1:9",
                   "HTTP_PROXY": "http://127.0.0.1:9", "NO_PROXY": ""}
        reader = InMemoryMetricReader()
        provider = MeterProvider(metric_readers=[reader])
        provider.get_meter("t").create_counter("digitalbrain.ai.cost").add(1.5)
        with patch.dict(os.environ, planted), override_settings(SECRET_DIRECTORY=secrets.name):
            sender = metrics.MetricSender({"endpoint": endpoint, "authorization": True})
            from opentelemetry.sdk.metrics.export import MetricExportResult

            self.assertEqual(sender.export(reader.get_metrics_data()),
                             MetricExportResult.SUCCESS)
        ((path, headers, body),) = Collector.received
        headers = {key.lower(): value for key, value in headers.items()}
        self.assertEqual(path, "/v1/metrics")
        self.assertEqual(headers["authorization"], "Bearer collector-token")
        self.assertNotIn("x-leak", headers)
        sent = ExportMetricsServiceRequest.FromString(gzip.decompress(body))
        names = [m.name for r in sent.resource_metrics for s in r.scope_metrics
                 for m in s.metrics]
        self.assertEqual(names, ["digitalbrain.ai.cost"])


class ResourceTests(SimpleTestCase):
    def test_each_process_reports_as_its_own_instance(self):
        """Two processes with one service name used to export identical series,
        and the collector kept the last: 100 tokens recorded twice read as 100."""
        from platform_core import telemetry

        config = {"endpoint": "http://collector:4318", "service_name": "brain"}
        provider = metrics.build(config)
        try:
            attributes = dict(provider._sdk_config.resource.attributes)
        finally:
            provider.shutdown()
        self.assertEqual(attributes["service.name"], "brain")
        self.assertEqual(attributes["service.instance.id"], telemetry.INSTANCE)
        self.assertEqual(telemetry.resource(config).attributes, attributes)
        self.assertEqual(str(uuid.UUID(telemetry.INSTANCE)), telemetry.INSTANCE)


class MetricSettingTests(SimpleTestCase):
    BASE = OtelSettingTests.BASE
    load = OtelSettingTests.load

    def test_metrics_settings(self):
        loaded = self.load('[otel]\nendpoint = "http://collector:4318"\nmetrics = false\n'
                           "metric_interval_seconds = 30\n")
        self.assertIs(loaded["otel"]["metrics"], False)
        for body in ('[otel]\nendpoint = "http://c:4318"\nmetrics = "yes"\n',
                     '[otel]\nendpoint = "http://c:4318"\nmetric_interval_seconds = 5\n',
                     '[otel]\nendpoint = "http://c:4318"\nmetric_interval_seconds = 1.5\n'):
            with self.assertRaises(ImproperlyConfigured, msg=body):
                self.load(body)
