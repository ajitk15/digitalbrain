"""Rates, errors and durations of requests, lane steps, paid calls and answers.

The same `[otel]` table that sends traces sends these (`metrics = false`
turns them off on their own); without it nothing here imports the SDK and
every `record` is a no-op. They go to the same collector through the same
exporter (`telemetry.Exporter`), so there is one endpoint, one CA and one
mounted credential, and nothing is read from `OTEL_*` variables. The global
meter provider is never set, for the reason the tracer provider is not.

What an instrument may carry is the closed registry `INSTRUMENTS`, each with
the attribute keys it accepts - a subset of `telemetry.ATTRIBUTES`, so a
metric can say nothing a span may not. Two more rules a span does not need:

* **No per-request value is a label.** A request id, a response id or a
  token count would make a new series per call; they are trace attributes,
  never metric attributes. The registry has no instrument that takes one.
* **Only spend carries an application id.** Queue age, latency and errors
  are platform figures; spend rate per tenant is the one figure the design
  asks for by tenant, and its cardinality is the number of applications.

Gauges that need the database - each lane's queue, calls in flight - are read
by the maintenance lane (`observe`, a step) and held here for the reader, so
only the process holding that lane reports them and a collector sees one
series, not one per process. Answers streaming is per process, and each
process reports its own.

Service levels are measured from these and from the platform's own records:
see `slo.py`.
"""

import threading
import time
from dataclasses import dataclass
from decimal import Decimal

from django.conf import settings

from . import telemetry

#: Bucket bounds in seconds, from 5 ms to five minutes: a page, a lane step
#: and a paid call all fall inside, and the SLOs' thresholds are bounds.
SECONDS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300)
#: Seconds between exports, unless `[otel] metric_interval_seconds` says.
INTERVAL = 60
#: How often the maintenance lane re-reads the queue figures.
OBSERVE_SECONDS = 60


@dataclass(frozen=True)
class Instrument:
    kind: str
    unit: str
    description: str
    attributes: frozenset


def instrument(kind, unit, description, *keys):
    return Instrument(kind, unit, description, frozenset(telemetry.dotted(dict.fromkeys(keys))))


#: Everything this platform measures. A name or a key outside it is dropped.
INSTRUMENTS = {
    "http.server.request.duration": instrument(
        "histogram", "s", "How long a request took until its view returned.",
        "http.request.method", "http.route", "http.response.status_code", "streamed",
    ),
    "digitalbrain.lane.step.duration": instrument(
        "histogram", "s", "How long a lane step that did work took.",
        "lane", "outcome",
    ),
    "digitalbrain.ai.admission.wait": instrument(
        "histogram", "s", "How long a paid call waited for a gateway slot.",
        "purpose", "provider", "outcome",
    ),
    "gen_ai.client.operation.duration": instrument(
        "histogram", "s", "How long an admitted paid call held its slot.",
        "purpose", "provider", "model", "outcome",
    ),
    "gen_ai.client.token.usage": instrument(
        "counter", "{token}", "Tokens on recorded receipts.",
        "purpose", "provider", "model", "token_type",
    ),
    "digitalbrain.ai.cost": instrument(
        "counter", "{currency}", "Spend on recorded receipts, by application.",
        "application_id", "purpose", "provider", "currency",
    ),
    "digitalbrain.answer.duration": instrument(
        "histogram", "s", "From a question being asked to its answer ending.",
        "mode", "provider", "outcome",
    ),
    "digitalbrain.queue.waiting": instrument(
        "gauge", "{item}", "Work each lane would take next.", "lane",
    ),
    "digitalbrain.queue.oldest_age": instrument(
        "gauge", "s", "Age of the longest-waiting work in each lane.", "lane",
    ),
    "digitalbrain.queue.dead": instrument(
        "gauge", "{job}", "Jobs that used every attempt, by lane.", "lane",
    ),
    "digitalbrain.lane.heartbeat_age": instrument(
        "gauge", "s", "Seconds since each lane's holder last wrote its heartbeat.", "lane",
    ),
    "digitalbrain.ai.in_flight": instrument(
        "gauge", "{call}", "Paid calls holding a gateway slot, by provider.", "provider",
    ),
    "digitalbrain.answers.streaming": instrument(
        "gauge", "{answer}", "Answers this process is streaming.",
    ),
    "digitalbrain.slo.events": instrument(
        "gauge", "{event}", "Good and bad events in each records objective's window.",
        "objective", "outcome",
    ),
}
GAUGES = {name for name, made in INSTRUMENTS.items() if made.kind == "gauge"}


def labels(name, attributes):
    """Only the keys `name` takes, only plain values; a UUID is its text."""
    allowed = INSTRUMENTS[name].attributes
    return {key: value for key, value in telemetry.clean(telemetry.dotted(attributes)).items()
            if key in allowed}


_meter = None
_provider = None
_built_for = None
_made = {}
_held = {}
_lock = threading.Lock()


def enabled():
    config = getattr(settings, "OTEL", None) or {}
    return bool(config) and config.get("metrics", True) is not False


def meter():
    """This process's instruments, built on first use; None when metrics are off."""
    global _meter, _provider, _built_for
    if not enabled():
        return None
    config = settings.OTEL
    with _lock:
        if _built_for != config:
            if _provider is not None:
                _provider.shutdown()
            _provider = build(config)
            _meter = made(_provider)
            _built_for = dict(config)
        return _meter


def use(provider):
    """For tests: measure through `provider` (None to read the settings again)."""
    global _meter, _provider, _built_for
    with _lock:
        _provider = provider
        _meter = made(provider) if provider is not None else None
        _built_for = getattr(settings, "OTEL", None) if provider is not None else None


def made(provider):
    """Every registered instrument on `provider`, by name."""
    from opentelemetry.metrics import Observation

    scope = provider.get_meter("digital-brain")
    instruments = {}
    for name, spec in INSTRUMENTS.items():
        if spec.kind == "histogram":
            instruments[name] = scope.create_histogram(name, spec.unit, spec.description)
        elif spec.kind == "counter":
            instruments[name] = scope.create_counter(name, spec.unit, spec.description)
        else:
            def observe(options, name=name):
                return [Observation(value, dict(key)) for key, value in held(name).items()]

            instruments[name] = scope.create_observable_gauge(
                name, [observe], spec.unit, spec.description
            )
    return instruments


def record(name, value, **attributes):
    """Add `value` to a histogram or counter; nothing when metrics are off."""
    instruments = meter()
    if instruments is None or name not in INSTRUMENTS or name in GAUGES:
        return
    if isinstance(value, Decimal):
        value = float(value)
    if INSTRUMENTS[name].kind == "counter":
        instruments[name].add(value, labels(name, attributes))
    else:
        instruments[name].record(value, labels(name, attributes))


def hold(name, values):
    """Replace what gauge `name` reports: [(attributes, value)]."""
    if name not in GAUGES:
        return
    with _lock:
        _held[name] = {
            tuple(sorted(labels(name, attributes).items())): value
            for attributes, value in values
        }


def held(name):
    with _lock:
        if name == "digitalbrain.answers.streaming":
            from .agent_runtime.streaming import streaming_count

            return {(): streaming_count()}
        return dict(_held.get(name, {}))


_observed = 0.0


def observe():
    """Read each lane's queue and the calls in flight for the gauges. A maintenance step.

    Returns False like the other figures-only steps: reading is not work.
    """
    global _observed
    if meter() is None or time.monotonic() - _observed < OBSERVE_SECONDS:
        return False
    _observed = time.monotonic()
    from .gateway import in_flight
    from .queue_status import lane_status

    rows = lane_status()
    hold("digitalbrain.queue.waiting", [({"lane": r["lane"]}, r["waiting"]) for r in rows])
    hold("digitalbrain.queue.oldest_age",
         [({"lane": r["lane"]}, r["oldest_seconds"] or 0) for r in rows])
    hold("digitalbrain.queue.dead", [({"lane": r["lane"]}, r["dead"]) for r in rows])
    hold("digitalbrain.lane.heartbeat_age", [
        ({"lane": r["lane"]}, r["heartbeat_seconds"])
        for r in rows if r["heartbeat_seconds"] is not None
    ])
    hold("digitalbrain.ai.in_flight",
         [({"provider": provider}, count) for provider, count in in_flight().items()])
    return False


def build(config):
    from opentelemetry.sdk.metrics import AlwaysOffExemplarFilter, MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

    interval = int(config.get("metric_interval_seconds", INTERVAL))
    return MeterProvider(
        metric_readers=[PeriodicExportingMetricReader(
            MetricSender(config),
            # Stated, so OTEL_METRIC_EXPORT_INTERVAL and _TIMEOUT are not read.
            export_interval_millis=interval * 1000,
            export_timeout_millis=telemetry.EXPORT_SECONDS * 1000,
        )],
        resource=telemetry.resource(config),
        # An exemplar carries a trace id onto a metric point; stated off, so
        # OTEL_METRICS_EXEMPLAR_FILTER is not read either.
        exemplar_filter=AlwaysOffExemplarFilter(),
        views=views(),
        shutdown_on_exit=False,
    )


def views():
    """Every histogram in seconds, bucketed at `SECONDS` rather than the SDK's milliseconds."""
    from opentelemetry.sdk.metrics import Histogram
    from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View

    return [View(instrument_type=Histogram,
                 aggregation=ExplicitBucketHistogramAggregation(SECONDS))]


def MetricSender(config):
    """A metric exporter posting through `telemetry.Exporter` to `/v1/metrics`.

    Made here rather than at import, so a process with metrics off never
    imports the SDK. Cumulative temporality, which every Prometheus-style
    backend reads, stated so the SDK's environment variable is not.
    """
    from opentelemetry.sdk.metrics import Counter, Histogram, ObservableGauge
    from opentelemetry.sdk.metrics.export import (
        AggregationTemporality,
        MetricExporter,
        MetricExportResult,
    )

    class Sender(MetricExporter):
        def __init__(self):
            super().__init__(preferred_temporality={
                Counter: AggregationTemporality.CUMULATIVE,
                Histogram: AggregationTemporality.CUMULATIVE,
                ObservableGauge: AggregationTemporality.CUMULATIVE,
            })
            self.poster = telemetry.Exporter(config, "metrics")

        def export(self, metrics_data, timeout_millis=10_000, **kwargs):
            from opentelemetry.exporter.otlp.proto.common.metrics_encoder import (
                encode_metrics,
            )

            points = sum(
                len(metric.data.data_points)
                for resource in metrics_data.resource_metrics
                for scope in resource.scope_metrics
                for metric in scope.metrics
            )
            if self.poster.send(encode_metrics(metrics_data), points):
                return MetricExportResult.SUCCESS
            return MetricExportResult.FAILURE

        def force_flush(self, timeout_millis=10_000):
            return True

        def shutdown(self, timeout_millis=30_000, **kwargs):
            self.poster.shutdown()

    return Sender()
