"""Traces (`telemetry.py`): off by default, ids and timings only when on.

Spans are caught in memory for most of this, through the same processor that
drops idle lane steps; the exporter is checked against a real HTTP listener on
this machine, with the environment planted to pull it elsewhere.
"""

import gzip
import http.server
import json
import logging
import os
import tempfile
import threading
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from digitalbrain.configuration import load_config
from platform_core import document_worker, metrics, telemetry
from platform_core.ai import invoke_ai
from platform_core.models import AIConfiguration
from platform_core.observability import SafeJsonFormatter
from platform_core.workbench import add_knowledge

from . import test_claude_messages, test_documents

COLLECTOR = {"endpoint": "http://127.0.0.1:4318"}
QUESTION = "What is the refund deadline for patient MRN 4471?"


class Traced:
    """Spans in memory, through the platform's own idle filter."""

    def start_tracing(self):
        self.spans = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(telemetry.drop_idle(SimpleSpanProcessor(self.spans)))
        on = override_settings(OTEL=COLLECTOR)
        on.enable()
        self.addCleanup(on.disable)
        telemetry.use(provider)
        self.addCleanup(telemetry.use, None)
        # Metrics come on with the same table; held in memory, never exported.
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import InMemoryMetricReader

        self.measured = InMemoryMetricReader()
        metrics.use(MeterProvider(metric_readers=[self.measured], views=metrics.views()))
        self.addCleanup(metrics.use, None)

    def finished(self):
        return self.spans.get_finished_spans()

    def assertOnlyRegistered(self, spans):
        for span in spans:
            self.assertLessEqual(set(span.attributes), telemetry.ATTRIBUTES, span.name)
            for event in span.events:
                self.assertLessEqual(set(event.attributes), telemetry.ATTRIBUTES, event.name)

    def everything_said(self, spans):
        said = []
        for span in spans:
            said += [span.name, *map(str, span.attributes.values())]
            for event in span.events:
                said += [event.name, *map(str, event.attributes.values())]
        return " ".join(said)


class OffTests(SimpleTestCase):
    @override_settings(OTEL={})
    def test_without_an_otel_table_nothing_is_traced(self):
        telemetry.use(None)
        self.assertIsNone(telemetry.tracer())
        with telemetry.span("anything", lane="intake") as traced:
            traced.set(idle=True)
        self.assertIs(traced, telemetry.NOTHING)
        self.assertIs(telemetry.current(), telemetry.NOTHING)
        self.assertIsNone(telemetry.trace_id())


class SpanTests(Traced, SimpleTestCase):
    def setUp(self):
        self.start_tracing()

    def test_only_registered_keys_and_plain_values_are_kept(self):
        kept = telemetry.clean({
            "digitalbrain.lane": "intake",
            "question": QUESTION,
            "http.url": "https://brain.example/applications/x/?q=secret",
            "gen_ai.usage.input_tokens": 12,
            "digitalbrain.application_id": None,
            "digitalbrain.outcome": {"nested": "dict"},
        })
        self.assertEqual(kept, {"digitalbrain.lane": "intake", "gen_ai.usage.input_tokens": 12})

    def test_a_failure_is_recorded_by_its_class_name_never_its_message(self):
        with self.assertRaises(ValueError):
            with telemetry.span("step"):
                raise ValueError(QUESTION)
        (span,) = self.finished()
        self.assertEqual(span.attributes["error.type"], "ValueError")
        self.assertEqual(span.events, ())
        self.assertNotIn("MRN", self.everything_said([span]) + str(span.status.description))

    def test_a_lane_step_that_found_nothing_is_dropped(self):
        stop = threading.Event()
        calls = []

        def found_work():
            calls.append("work")
            return True

        def found_nothing():
            calls.append("idle")
            if calls.count("idle") >= 2:
                stop.set()
            return False

        def broke():
            raise RuntimeError("row it cannot read")

        with self.assertLogs("platform_core.document_worker", "WARNING"):
            document_worker.run_lane(
                "intake", lambda: (found_work, found_nothing, broke), 0, stop
            )
        names = [span.name for span in self.finished()]
        self.assertIn("intake found_work", names)
        self.assertNotIn("intake found_nothing", names)
        failed = [span for span in self.finished() if span.name == "intake broke"]
        self.assertEqual(failed[0].attributes["error.type"], "RuntimeError")
        self.assertEqual(failed[0].attributes["digitalbrain.lane"], "intake")
        self.assertOnlyRegistered(self.finished())

    def test_a_log_line_names_its_trace(self):
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "m", None, None)
        with telemetry.span("request"):
            inside = json.loads(SafeJsonFormatter().format(record))
        outside = json.loads(SafeJsonFormatter().format(record))
        (span,) = self.finished()
        self.assertEqual(inside["trace_id"], format(span.context.trace_id, "032x"))
        self.assertNotIn("trace_id", outside)


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    CLAUDE_MESSAGES_API=True,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class PlatformSpanTests(Traced, TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.source = add_knowledge(
            self.owner, self.app.pk, "Policy", "Refunds are accepted for thirty days."
        )
        self.start_tracing()

    def test_a_request_is_its_method_route_pattern_status_and_ids(self):
        response = self.client.get(reverse("documents", args=[self.app.pk]) + "?q=MRN+4471")
        self.assertEqual(response.status_code, 200)
        (span,) = [s for s in self.finished() if s.name.startswith("GET ")]
        self.assertEqual(span.name, "GET applications/<uuid:pk>/documents/")
        self.assertEqual(span.attributes["http.route"], "applications/<uuid:pk>/documents/")
        self.assertEqual(span.attributes["http.response.status_code"], 200)
        self.assertEqual(span.attributes["digitalbrain.application_id"], str(self.app.pk))
        self.assertEqual(span.attributes["digitalbrain.request_id"], response["X-Request-ID"])
        self.assertNotIn("4471", self.everything_said([span]))
        self.assertOnlyRegistered([span])

    def test_a_paid_call_is_its_purpose_model_wait_outcome_and_receipt(self):
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
        (span,) = [s for s in self.finished() if s.name == "ai chat"]
        self.assertEqual(span.attributes["gen_ai.operation.name"], "chat")
        self.assertEqual(span.attributes["gen_ai.provider.name"], "claude")
        self.assertEqual(span.attributes["gen_ai.request.model"], "claude-sonnet-5")
        self.assertEqual(span.attributes["digitalbrain.outcome"], "answered")
        self.assertGreaterEqual(span.attributes["digitalbrain.wait_ms"], 0)
        (receipt,) = span.events
        self.assertEqual(receipt.name, "ai.receipt")
        self.assertEqual(receipt.attributes["gen_ai.response.id"], "msg_01test")
        self.assertEqual(receipt.attributes["gen_ai.usage.input_tokens"], 150)
        said = self.everything_said(self.finished())
        for private in ("4471", "refund", "thirty days", test_claude_messages.KEY):
            self.assertNotIn(private, said.lower())
        self.assertOnlyRegistered(self.finished())


class Collector(http.server.BaseHTTPRequestHandler):
    received = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        Collector.received.append((self.path, dict(self.headers), body))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


class ExporterTests(SimpleTestCase):
    def listen(self):
        server = http.server.HTTPServer(("127.0.0.1", 0), Collector)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        Collector.received = []
        return f"http://127.0.0.1:{server.server_port}"

    def spans(self):
        memory = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(memory))
        with provider.get_tracer("t").start_as_current_span("GET applications/<uuid:pk>/"):
            pass
        return memory.get_finished_spans()

    def test_spans_reach_the_configured_collector_and_nothing_from_the_environment(self):
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceRequest,
        )

        endpoint = self.listen()
        secrets = tempfile.TemporaryDirectory()
        self.addCleanup(secrets.cleanup)
        token = Path(secrets.name) / "otel_authorization"
        token.write_text("Bearer collector-token", encoding="utf-8")
        os.chmod(token, 0o600)
        planted = {
            "OTEL_EXPORTER_OTLP_HEADERS": "x-leak=operator,authorization=Bearer operator",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:9",
            "OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE": "/nonexistent.pem",
            "HTTP_PROXY": "http://127.0.0.1:9",
            "NO_PROXY": "",
        }
        with patch.dict(os.environ, planted), override_settings(SECRET_DIRECTORY=secrets.name):
            exporter = telemetry.Exporter({"endpoint": endpoint, "authorization": True})
            self.assertEqual(exporter.export(self.spans()), SpanExportResult.SUCCESS)
        ((path, headers, body),) = Collector.received
        headers = {key.lower(): value for key, value in headers.items()}
        self.assertEqual(path, "/v1/traces")
        self.assertEqual(headers["authorization"], "Bearer collector-token")
        self.assertNotIn("x-leak", headers)
        sent = ExportTraceServiceRequest.FromString(gzip.decompress(body))
        names = [s.name for r in sent.resource_spans for ss in r.scope_spans for s in ss.spans]
        self.assertEqual(names, ["GET applications/<uuid:pk>/"])

    def test_a_collector_that_is_down_costs_one_warning_not_a_retry_storm(self):
        exporter = telemetry.Exporter({"endpoint": "http://127.0.0.1:9"})
        with self.assertLogs("platform_core.telemetry", "WARNING") as logged:
            self.assertEqual(exporter.export(self.spans()), SpanExportResult.FAILURE)
            self.assertEqual(exporter.export(self.spans()), SpanExportResult.FAILURE)
        self.assertEqual(len(logged.records), 1)
        self.assertEqual(logged.records[0].event, "otel_export_failed")


class OtelSettingTests(SimpleTestCase):
    BASE = 'hosts = ["localhost"]\nsecret_directory = "/tmp/secrets"\n'

    def load(self, body, mode="development"):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(f'mode = "{mode}"\n' + self.BASE + body, encoding="utf-8")
            with patch.dict("os.environ", {"DIGITAL_BRAIN_CONFIG": str(path)}):
                return load_config()

    def test_off_unless_a_collector_is_named(self):
        self.assertEqual(self.load("").get("otel", {}), {})
        loaded = self.load('[otel]\nendpoint = "http://collector:4318"\nsample_ratio = 0.25\n')
        self.assertEqual(loaded["otel"]["sample_ratio"], 0.25)

    def test_what_is_refused(self):
        for body, mode in (
            ('[otel]\nendpoint = "http://collector:4318"\n', "production"),
            ('[otel]\nendpoint = "https://collector:4318/v1/traces?x=1"\n', "development"),
            ('[otel]\nendpoint = "https://u:p@collector:4318"\n', "development"),
            ('[otel]\nendpoint = "https://collector"\nheaders = "a=b"\n', "development"),
            ('[otel]\nendpoint = "https://collector"\nsample_ratio = 0\n', "development"),
            ('[otel]\nservice_name = "x"\n', "development"),
        ):
            with self.assertRaises(ImproperlyConfigured, msg=body):
                self.load(body, mode)
