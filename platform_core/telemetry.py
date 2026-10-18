"""Traces of requests, lane steps and paid model calls: ids and timings only, off by default.

An `[otel]` table in the configuration turns it on; without one nothing here
imports the OpenTelemetry SDK and every `span` is a no-op. The global tracer
provider is never set, on or off, so a library that traces through the API
on its own - the MCP SDK does - records nothing: only the spans below exist.
With a table, three things are traced (CLAUDE.md, "Traces"):

* every HTTP request (`TelemetryMiddleware`) - its method, the route's
  *pattern* (`applications/<uuid:pk>/chat/`, never the path or a query), the
  status, the request id and, under an application, that application's id;
* every lane step that did work (`document_worker.run_lane`) - the lane and
  the step's function name; a step that found nothing is dropped
  (`drop_idle`), or nine lanes ticking would be all a trace store held;
* every paid call through the gateway (`gateway.admitted`) - purpose,
  provider, model, application, how long admission waited, the outcome, and
  for each receipt its provider request id and token counts.

What a span may carry is a closed registry (`ATTRIBUTES`), like the log
formatter's allowlist: a key outside it is dropped, so a prompt, an answer,
a source's text, a name, an email or a URL cannot reach a collector by
accident. Exceptions are recorded by class name only - a message may quote
anything. No context is read from incoming headers (a caller cannot join its
trace to ours) and none is sent with outgoing calls (a provider learns no
trace id).

The exporter is OTLP over HTTP, protobuf, to the configured `endpoint`, written
here over the SDK's own encoder rather than taken from the OTLP exporter
package: that one merges `OTEL_EXPORTER_OTLP_HEADERS`, client certificates
and a CA from the environment whatever it is given, and here the collector,
its CA and its credential are the operator's configuration and mounted file
(`otel_authorization`) alone. Spans are batched off the request thread and
dropped, never queued without bound, when the collector is down.
"""

import gzip
import logging
import re
import threading
import time
import uuid
from contextlib import contextmanager

from django.conf import settings

logger = logging.getLogger(__name__)

#: Everything a span may say. Ids, labels, counts and timings - nothing a
#: person wrote, nothing a model wrote.
ATTRIBUTES = frozenset({
    "http.request.method",
    "http.route",
    "http.response.status_code",
    "digitalbrain.request_id",
    "digitalbrain.application_id",
    "digitalbrain.streamed",
    "digitalbrain.lane",
    "digitalbrain.step",
    "digitalbrain.idle",
    "digitalbrain.job_kind",
    "digitalbrain.outcome",
    "digitalbrain.wait_ms",
    "gen_ai.operation.name",
    "gen_ai.provider.name",
    "gen_ai.request.model",
    "gen_ai.response.id",
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
    "error.type",
})
METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
#: Seconds an export may take, and between warnings that the collector is down.
EXPORT_SECONDS = 10
WARN_EVERY_SECONDS = 300


def clean(attributes):
    """Only registered keys, only plain values; a UUID is its text."""
    kept = {}
    for key, value in attributes.items():
        if key not in ATTRIBUTES or value is None:
            continue
        if isinstance(value, uuid.UUID):
            value = str(value)
        if isinstance(value, (str, bool, int, float)):
            kept[key] = value
    return kept


class Span:
    """The part of a span this platform uses."""

    def __init__(self, span=None):
        self._span = span

    def set(self, **attributes):
        if self._span is not None:
            self._span.set_attributes(clean(dotted(attributes)))

    def event(self, name, **attributes):
        if self._span is not None:
            self._span.add_event(name, clean(dotted(attributes)))

    def rename(self, name):
        if self._span is not None:
            self._span.update_name(name)


NOTHING = Span()


def dotted(attributes):
    """`application_id=` for `digitalbrain.application_id`, so callers stay short."""
    return {SHORT.get(key, key): value for key, value in attributes.items()}


SHORT = {
    "application_id": "digitalbrain.application_id",
    "request_id": "digitalbrain.request_id",
    "lane": "digitalbrain.lane",
    "step": "digitalbrain.step",
    "idle": "digitalbrain.idle",
    "job_kind": "digitalbrain.job_kind",
    "outcome": "digitalbrain.outcome",
    "wait_ms": "digitalbrain.wait_ms",
    "streamed": "digitalbrain.streamed",
    "purpose": "gen_ai.operation.name",
    "provider": "gen_ai.provider.name",
    "model": "gen_ai.request.model",
    "response_id": "gen_ai.response.id",
    "input_tokens": "gen_ai.usage.input_tokens",
    "output_tokens": "gen_ai.usage.output_tokens",
}


_tracer = None
_provider = None
_built_for = None
_lock = threading.Lock()


def enabled():
    return bool(getattr(settings, "OTEL", None))


def tracer():
    """This process's tracer, built on first use; None when tracing is off."""
    global _tracer, _provider, _built_for
    config = getattr(settings, "OTEL", None) or None
    if not config:
        return None
    with _lock:
        if _built_for != config:
            if _provider is not None:
                _provider.shutdown()
            _provider = build(config)
            _tracer = _provider.get_tracer("digital-brain")
            _built_for = dict(config)
        return _tracer


def use(provider):
    """For tests: trace through `provider` (None to read the settings again)."""
    global _tracer, _provider, _built_for
    with _lock:
        _provider = provider
        _tracer = provider.get_tracer("digital-brain") if provider is not None else None
        _built_for = getattr(settings, "OTEL", None) if provider is not None else None


@contextmanager
def span(name, **attributes):
    """A span around the block, current for whatever the block calls."""
    traced = tracer()
    if traced is None:
        yield NOTHING
        return
    from opentelemetry.trace import Status, StatusCode

    with traced.start_as_current_span(
        name,
        attributes=clean(dotted(attributes)),
        # A message or a traceback may quote anything; the class name may not.
        record_exception=False,
        set_status_on_exception=False,
    ) as current:
        try:
            yield Span(current)
        except BaseException as failure:
            current.set_attribute("error.type", type(failure).__name__)
            current.set_status(Status(StatusCode.ERROR))
            raise


def current():
    """The span the caller is inside, to add to; does nothing when off."""
    if tracer() is None:
        return NOTHING
    from opentelemetry import trace

    found = trace.get_current_span()
    return Span(found) if found.get_span_context().is_valid else NOTHING


def trace_id():
    """The current trace's id for a log line, or None."""
    # A log line may be formatted before settings exist (a script's own setup).
    if _tracer is None or not settings.configured or not enabled():
        return None
    from opentelemetry import trace

    context = trace.get_current_span().get_span_context()
    return format(context.trace_id, "032x") if context.is_valid else None


def build(config):
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

    provider = TracerProvider(
        # Stated, so OTEL_TRACES_SAMPLER and OTEL_RESOURCE_ATTRIBUTES are not read.
        sampler=ParentBased(TraceIdRatioBased(float(config.get("sample_ratio", 1.0)))),
        resource=Resource({
            "service.name": config.get("service_name", "digital-brain"),
            "deployment.environment.name": "production" if settings.PRODUCTION
            else "development",
        }),
    )
    provider.add_span_processor(drop_idle(BatchSpanProcessor(
        Exporter(config),
        max_queue_size=2048,
        schedule_delay_millis=5000,
        max_export_batch_size=256,
        export_timeout_millis=EXPORT_SECONDS * 1000,
    )))
    return provider


def drop_idle(inner):
    """A span processor that passes on every span but a lane step that found nothing.

    Made here rather than at import, so a process with tracing off never
    imports the SDK.
    """
    from opentelemetry.sdk.trace import SpanProcessor

    class DropIdle(SpanProcessor):
        def on_start(self, span, parent_context=None):
            inner.on_start(span, parent_context=parent_context)

        def on_end(self, span):
            if not (span.attributes or {}).get("digitalbrain.idle"):
                inner.on_end(span)

        def shutdown(self):
            inner.shutdown()

        def force_flush(self, timeout_millis=30000):
            return inner.force_flush(timeout_millis)

    return DropIdle()


def authorization():
    from digitalbrain.configuration import read_secret

    return read_secret(settings.SECRET_DIRECTORY, "otel_authorization")


class Exporter:
    """OTLP/HTTP protobuf to the configured collector, and nowhere else.

    One attempt per batch: a trace is not worth a retry storm against a
    collector that is down, and the batch processor already drops what it
    cannot hold.
    """

    def __init__(self, config):
        import requests

        self.url = config["endpoint"].rstrip("/") + "/v1/traces"
        self.verify = config.get("ca_certificate") or True
        self.headers = {
            "Content-Type": "application/x-protobuf",
            "Content-Encoding": "gzip",
        }
        if config.get("authorization"):
            self.headers["Authorization"] = authorization()
        self.session = requests.Session()
        # Not HTTPS_PROXY, not REQUESTS_CA_BUNDLE, not ~/.netrc.
        self.session.trust_env = False
        self.warned = 0.0

    def export(self, spans):
        from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
        from opentelemetry.sdk.trace.export import SpanExportResult

        body = gzip.compress(encode_spans(spans).SerializeToString())
        try:
            answer = self.session.post(
                self.url, data=body, headers=self.headers, verify=self.verify,
                timeout=EXPORT_SECONDS, allow_redirects=False,
            )
            ok = 200 <= answer.status_code < 300
            failure = "" if ok else f"http_{answer.status_code}"
        except Exception as error:
            ok, failure = False, type(error).__name__
        if ok:
            return SpanExportResult.SUCCESS
        if time.monotonic() - self.warned > WARN_EVERY_SECONDS:
            self.warned = time.monotonic()
            logger.warning(
                "otel_export_failed",
                extra={"event": "otel_export_failed", "failure_kind": failure,
                       "count": len(spans)},
            )
        return SpanExportResult.FAILURE

    def shutdown(self):
        self.session.close()

    def force_flush(self, timeout_millis=30000):
        return True


APPLICATION_ROUTE = re.compile(r"^(api/v1/)?applications/")


class TelemetryMiddleware:
    """One span per request: method, route pattern, status, request and application id."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not enabled():
            return self.get_response(request)
        method = request.method if request.method in METHODS else "_OTHER"
        with span(method, **{"http.request.method": method}) as traced:
            response = self.get_response(request)
            match = getattr(request, "resolver_match", None)
            route = getattr(match, "route", None) or ""
            if route:
                traced.rename(f"{method} {route}")
            pk = (getattr(match, "kwargs", None) or {}).get("pk")
            traced.set(**{
                "http.route": route or None,
                "http.response.status_code": response.status_code,
                "request_id": getattr(request, "request_id", None),
                "application_id": pk if APPLICATION_ROUTE.match(route)
                and isinstance(pk, uuid.UUID) else None,
                "streamed": bool(getattr(response, "streaming", False)),
            })
            return response
