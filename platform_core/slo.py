"""What this platform promises its users, as numbers it can check.

A closed registry like `services.FEATURES`: each objective names what it
promises in words, the share of events that must be good, and how an event is
counted. Two kinds, because they are read from two places:

* **records** objectives are counted from rows this platform already keeps -
  an answer's status and its two timestamps, a job's due, start and finish -
  so `manage.py slo` measures them with no collector at all, and `--check`
  fails when one is below its target. The maintenance lane reads them every
  `READ_SECONDS` for the `digitalbrain.slo.events` gauge and logs them as
  `slo_status` events, counts only.
* **metrics** objectives need what only a running process sees - a request's
  status and duration, a paid call turned away, a queue's age minute by
  minute - so they are written as Prometheus rules over the metrics
  `metrics.py` exports (`prometheus_rules`, committed as
  `deploy/slo-rules.yml`, which a test holds equal to the registry).

Both kinds are judged over `WINDOW_DAYS`. An objective with fewer than
`MIN_EVENTS` events in its window is reported and never fails a check: two
answers, one failed, is not a 50% service.

Every count runs unbound by tenant (maintenance and management commands do):
these are the platform's figures, and they hold counts and ratios only.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta

from django.db.models import F
from django.utils import timezone

logger = logging.getLogger(__name__)

WINDOW_DAYS = 28
MIN_EVENTS = 20
#: How often the maintenance lane counts the records objectives again.
READ_SECONDS = 900
#: An answer is on time when it ended this soon after it was asked.
ANSWER_SECONDS = 60
#: Background work is on time when it started this soon after it fell due.
START_SECONDS = 300
#: A page is fast under this many seconds; a bucket bound in `metrics.SECONDS`.
PAGE_SECONDS = 1
#: A lane's oldest waiting work is fresh under this many seconds.
QUEUE_SECONDS = 600


@dataclass(frozen=True)
class Objective:
    key: str
    promise: str
    target: float
    kind: str
    #: records: (since, now) -> (good, total). metrics: PromQL with `{w}`.
    count: object = None
    errors: str = ""
    total: str = ""
    notes: str = field(default="", compare=False)


def answers_finish(since, now):
    """Answers that ended in the window: good if complete. A person's Stop is neither."""
    from .models import ChatMessage

    ended = ChatMessage.objects.filter(
        role="assistant", finished_at__gte=since, finished_at__lt=now,
        status__in=["complete", "failed"],
    )
    return ended.filter(status="complete").count(), ended.count()


def answers_in_time(since, now):
    """Complete answers in the window: good if they ended `ANSWER_SECONDS` after asking."""
    from .models import ChatMessage

    done = ChatMessage.objects.filter(
        role="assistant", status="complete", finished_at__gte=since, finished_at__lt=now
    )
    quick = done.filter(finished_at__lte=F("created_at") + timedelta(seconds=ANSWER_SECONDS))
    return quick.count(), done.count()


def work_starts(since, now):
    """Jobs due in the window: good if started `START_SECONDS` after falling due.

    A job still waiting past that is late already, and counts now; one still
    inside it is not yet judged.
    """
    from .models import Job

    late = now - timedelta(seconds=START_SECONDS)
    due = Job.objects.filter(run_after__gte=since, run_after__lt=late)
    started = due.filter(started_at__isnull=False)
    on_time = started.filter(started_at__lte=F("run_after") + timedelta(seconds=START_SECONDS))
    waiting = due.filter(started_at__isnull=True, status="queued")
    return on_time.count(), started.count() + waiting.count()


def work_succeeds(since, now):
    """Jobs that ended in the window: good if done, bad if they used every attempt."""
    from .models import Job

    ended = Job.objects.filter(
        finished_at__gte=since, finished_at__lt=now, status__in=["done", "dead"]
    )
    return ended.filter(status="done").count(), ended.count()


def label(key):
    """A metric attribute's name as Prometheus reads it: `lane` is `digitalbrain_lane`.

    The rules used to write `lane=` and `objective=`, which no exported series
    carries, so the queue and records alerts matched nothing and could never fire.
    """
    from .telemetry import dotted

    return next(iter(dotted({key: None}))).replace(".", "_")


STREAMED = f'{label("streamed")}="false"'
OUTCOME = label("outcome")
REQUESTS = "http_server_request_duration_seconds"
ADMISSIONS = "digitalbrain_ai_admission_wait_seconds"

#: Everything promised. Adding one here adds it to `manage.py slo`, the gauge,
#: the log and the rules file.
OBJECTIVES = (
    Objective(
        "answers_finish", "Answers end with an answer, not a failure.", 0.99, "records",
        count=answers_finish,
        notes="A person's Stop is not counted either way.",
    ),
    Objective(
        "answers_in_time",
        f"A complete answer ends within {ANSWER_SECONDS} seconds of being asked.",
        0.95, "records", count=answers_in_time,
        notes="The JavaScript-off path writes its answer when it ends, so it reads as instant.",
    ),
    Objective(
        "work_starts",
        f"Background work starts within {START_SECONDS // 60} minutes of falling due.",
        0.99, "records", count=work_starts,
        notes="Graph refreshes and scheduled imports - the work written as jobs.",
    ),
    Objective(
        "work_succeeds", "Background work finishes rather than using every attempt.", 0.99,
        "records", count=work_succeeds,
    ),
    Objective(
        "requests_available", "A request is answered without a server error.", 0.999,
        "metrics",
        errors=(f'sum(rate({REQUESTS}_count{{{STREAMED},http_response_status_code=~"5.."}}'
                "[{w}]))"),
        total=f"sum(rate({REQUESTS}_count{{{STREAMED}}}[{{w}}]))",
        notes="A stream's status is sent before its answer, so streams are judged by answers.",
    ),
    Objective(
        "pages_fast", f"A page is served in under {PAGE_SECONDS} second.", 0.95, "metrics",
        errors=(f'(sum(rate({REQUESTS}_count{{{STREAMED},http_request_method="GET"}}[{{w}}]))'
                f' - sum(rate({REQUESTS}_bucket{{{STREAMED},http_request_method="GET",'
                # A bound reads "1" or "1.0" depending on the backend's version.
                f'le=~"{PAGE_SECONDS}(.0)?"}}[{{w}}])))'),
        total=f'sum(rate({REQUESTS}_count{{{STREAMED},http_request_method="GET"}}[{{w}}]))',
    ),
    Objective(
        "ai_admitted", "A paid call is admitted rather than turned away busy.", 0.99, "metrics",
        errors=f'sum(rate({ADMISSIONS}_count{{{OUTCOME}="busy"}}[{{w}}]))',
        total=f"sum(rate({ADMISSIONS}_count[{{w}}]))",
        notes="Turned away means nothing was sent and nothing was charged.",
    ),
    Objective(
        "queues_fresh",
        f"Intake, graph and import work waits under {QUEUE_SECONDS // 60} minutes.",
        0.99, "metrics",
        errors=("max(avg_over_time((max(digitalbrain_queue_oldest_age_seconds"
                f'{{{label("lane")}=~"intake|graph|connectors"}}) > bool {QUEUE_SECONDS})'
                "[{w}:1m]))"),
        total="vector(1)",
        notes="Minutes in which the oldest waiting item was older than that.",
    ),
)
BY_KEY = {objective.key: objective for objective in OBJECTIVES}


def measure(now=None, days=WINDOW_DAYS):
    """One row per records objective: good, total, ratio, budget left and verdict."""
    now = now or timezone.now()
    since = now - timedelta(days=days)
    rows = []
    for objective in OBJECTIVES:
        if objective.kind != "records":
            continue
        good, total = objective.count(since, now)
        ratio = good / total if total else None
        allowed = (1 - objective.target) * total
        rows.append({
            "objective": objective.key,
            "promise": objective.promise,
            "target": objective.target,
            "good": good,
            "total": total,
            "ratio": ratio,
            # The share of the window's error budget not yet spent; below 0 is overspent.
            "budget_left": 1 - (total - good) / allowed if allowed else None,
            "judged": total >= MIN_EVENTS,
            "met": ratio is None or total < MIN_EVENTS or ratio >= objective.target,
        })
    return rows


_read = 0.0


def read_if_due():
    """Count the records objectives for the gauge and the log. A maintenance step."""
    global _read
    if time.monotonic() - _read < READ_SECONDS:
        return False
    _read = time.monotonic()
    from . import metrics

    rows = measure()
    events = []
    for row in rows:
        events += [({"objective": row["objective"], "outcome": "good"}, row["good"]),
                   ({"objective": row["objective"], "outcome": "bad"},
                    row["total"] - row["good"])]
        logger.info(
            "slo status",
            extra={"event": "slo_status", "objective": row["objective"], "good": row["good"],
                   "total": row["total"], "met": row["met"]},
        )
    metrics.hold("digitalbrain.slo.events", events)
    return False


#: Multi-window burn-rate alerts, as the SRE workbook sets them: page when an
#: hour spends 2% of a 28-day budget (14.4x), open a ticket at 5% in six hours.
BURN_ALERTS = (
    ("page", 14.4, "1h", "5m"),
    ("ticket", 6, "6h", "30m"),
)


def prometheus_rules():
    """The Prometheus rule file for every objective, as YAML text."""
    lines = ["# Generated by platform_core/slo.py - `manage.py slo --rules`. Do not edit.",
             "groups:"]
    windows = sorted({w for _, _, long, short in BURN_ALERTS for w in (long, short)},
                     key=lambda w: (w[-1], int(w[:-1])))
    lines += ["  - name: digital-brain-slo-ratios", "    rules:"]
    for objective in OBJECTIVES:
        if objective.kind != "metrics":
            continue
        for window in windows:
            expression = (f"({objective.errors.replace("{w}", window)}) / "
                          f"({objective.total.replace("{w}", window)})")
            lines += [f"      - record: digitalbrain:slo_errors:ratio_rate{window}",
                      f"        expr: {quoted(expression)}",
                      "        labels:",
                      f"          objective: {objective.key}"]
    lines += ["  - name: digital-brain-slo-alerts", "    rules:"]
    for objective in OBJECTIVES:
        if objective.kind == "metrics":
            budget = round(1 - objective.target, 6)
            for severity, burn, long, short in BURN_ALERTS:
                limit = round(burn * budget, 6)
                expression = " and ".join(
                    f'digitalbrain:slo_errors:ratio_rate{w}{{objective="{objective.key}"}}'
                    f" > {limit}" for w in (long, short)
                )
                lines += alert(objective, severity, expression, f"burning {burn}x over {long}")
        else:
            which = f'{label("objective")}="{objective.key}"'
            good = f'digitalbrain_slo_events{{{which},{OUTCOME}="good"}}'
            every = f"sum(digitalbrain_slo_events{{{which}}})"
            expression = (f"sum({good}) / {every} < {objective.target} "
                          f"and {every} >= {MIN_EVENTS}")
            lines += alert(objective, "ticket", expression,
                           f"below target over {WINDOW_DAYS} days")
    return "\n".join(lines) + "\n"


def alert(objective, severity, expression, what):
    name = "".join(part.title() for part in objective.key.split("_"))
    return [
        f"      - alert: SLO{name}{severity.title()}",
        f"        expr: {quoted(expression)}",
        "        for: 2m" if severity == "page" else "        for: 15m",
        "        labels:",
        f"          severity: {severity}",
        f"          objective: {objective.key}",
        "        annotations:",
        f"          summary: {quoted(objective.promise.rstrip('.') + ' - ' + what)}",
    ]


def quoted(text):
    return "'" + text.replace("'", "''") + "'"
