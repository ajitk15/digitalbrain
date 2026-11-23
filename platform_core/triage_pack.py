"""ServiceOps triage with tools and without, against the causes people confirmed.

    manage.py triage_pack <application id> --as <username> [--record] [--only tools]

Phase 3 of `documentation/engineering/agentic-ai-plan.md`: whether letting the
triage model look further than its evidence pack (`agent_runtime/ops_tools.py`)
puts the cause people later confirmed among its hypotheses more often, or higher,
than the one-shot call over the pack - and at what cost. **Charges apply**: each
case is asked twice through the application's own ServiceOps configuration,
credential and monthly budget, receipted as usual - one call one-shot, up to
`serviceops_triage.TOOL_TURNS` with tools. Run deliberately, never by the suite.

A case is an incident of this application whose cause a person confirmed (a
`TriageVerdict` with an `actual_cause`), the newest per incident. It is asked
exactly what triage asks - `triage_question`, `model_evidence`, the same
instructions - over the pack `evidence_pack` builds **without that incident's own
confirmed cause** (`own_confirmed=False`): with it, the answer would be in the
question. Causes people confirmed on *similar* incidents stay, as triage has them.

Scored by rules, from the hypotheses `_parsed_hypotheses` keeps (only those whose
quotes are found word for word): whether one cites the confirmed cause, at what
rank, and whether the cause was in front of the model at all. Nothing is written
but the receipts: no run, no hypothesis, no verdict.
"""

import json
import re
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured, ValidationError

ROOT = Path(__file__).resolve().parent.parent
RECORDS = ROOT / "benchmarks" / "triage"
MODES = ("one_shot", "tools")
#: Tools are worth turning on only at no more than this many times the one-shot
#: cost, and no worse on what is scored - Phase 1's bar.
COST_BAR = 2
MAX_CASES = 50


def cases(app, limit=MAX_CASES):
    """[(incident, cause)]: the newest confirmed cause per incident, both still answerable."""
    from .models import TriageVerdict
    from .serviceops import incident_queryset, verified

    incidents = incident_queryset(app)
    found, seen = [], set()
    verdicts = (
        TriageVerdict.objects.filter(
            hypothesis__run__application=app,
            actual_cause__isnull=False,
            confirmed_at__isnull=False,
        )
        .select_related("hypothesis__run__incident", "actual_cause")
        .order_by("-confirmed_at")
    )
    for verdict in verdicts:
        incident, cause = verdict.hypothesis.run.incident, verdict.actual_cause
        if incident.pk in seen or not incidents.filter(pk=incident.pk).exists():
            continue
        if not (verified(incident) and cause.active and cause.sensitivity != "restricted"):
            continue
        seen.add(incident.pk)
        found.append((incident, cause))
        if len(found) >= limit:
            break
    return found


def score(hypotheses, cause_id):
    """The rank (from 1) of the first hypothesis citing the confirmed cause, or None."""
    for rank, item in enumerate(hypotheses, 1):
        if any(citation["id"] == cause_id for citation in item["citations"]):
            return rank
    return None


def ask_case(user, app, incident, cause, mode, config):
    from .ai import cost
    from .serviceops_triage import _parsed_hypotheses, ask_model, evidence_pack

    pack, _ = evidence_pack(app, incident, own_confirmed=False)
    receipt, failure, answer, extra = {}, "", "", []
    started = time.monotonic()
    try:
        answer, extra = ask_model(user, app, incident, pack, receipt, agent=mode == "tools")
    except (ValidationError, ImproperlyConfigured, OSError, TimeoutError) as refused:
        failure = " ".join(getattr(refused, "messages", [str(refused)]))[:300]
    hypotheses = _parsed_hypotheses(answer, pack + extra) if answer else []
    cause_id = str(cause.pk)
    rank = score(hypotheses, cause_id)
    return {
        "incident": str(incident.pk),
        "title": incident.title[:120],
        "mode": mode,
        "rank": rank,
        "hypotheses": len(hypotheses),
        "cause_in_pack": any(row["id"] == cause_id for row in pack),
        "cause_read": any(row["id"] == cause_id for row in extra),
        "failure": failure,
        "prompt_tokens": receipt.get("prompt_tokens", 0),
        "completion_tokens": receipt.get("completion_tokens", 0),
        "turns": receipt.get("turns", 1 if receipt else 0),
        "tool_calls": receipt.get("tool_calls", 0),
        "cost": str(
            cost(config, receipt["prompt_tokens"], receipt["completion_tokens"])
            if config and receipt
            else 0
        ),
        "seconds": round(time.monotonic() - started, 1),
    }


def run(user, app, modes=MODES, limit=MAX_CASES):
    """Every case every way. Returns the record `save` writes."""
    from .models import AIConfiguration

    found = cases(app, limit)
    if not found:
        raise ValidationError(
            "No incident here has a cause people confirmed. Confirm causes on ServiceOps "
            "first - the pack measures triage against them."
        )
    config = AIConfiguration.objects.filter(
        application=app, purpose="serviceops_triage", enabled=True
    ).first()
    details = [
        ask_case(user, app, incident, cause, mode, config)
        for incident, cause in found
        for mode in modes
    ]
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "provider": getattr(config, "provider", ""),
        "model": getattr(config, "model", ""),
        "metrics": metrics(details, modes),
        "details": details,
    }


def metrics(details, modes=MODES):
    found = {}
    for mode in modes:
        rows = [row for row in details if row["mode"] == mode]
        ranks = [row["rank"] for row in rows if row["rank"]]
        found[mode] = {
            "cases": len(rows),
            "failed": sum(bool(row["failure"]) for row in rows),
            "cause_cited": len(ranks),
            "cause_first": sum(rank == 1 for rank in ranks),
            "mrr": round(sum(1 / rank for rank in ranks) / max(1, len(rows)), 3),
            "cause_in_evidence": sum(row["cause_in_pack"] or row["cause_read"] for row in rows),
            "cost": str(sum((Decimal(row["cost"]) for row in rows), Decimal("0"))),
            "turns": sum(row["turns"] for row in rows),
            "tool_calls": sum(row["tool_calls"] for row in rows),
            "seconds": round(sum(row["seconds"] for row in rows), 1),
        }
    return found


def verdict(found):
    """Whether tools clear the bar against one-shot, and why not if they do not."""
    if not {"one_shot", "tools"} <= set(found):
        return "Run both ways to compare."
    from .agent_measurements import NOTHING_COMPARED, nothing_compared

    if nothing_compared(found):
        return NOTHING_COMPARED
    alone, tools = found["one_shot"], found["tools"]
    reasons = []
    if tools["cause_cited"] < alone["cause_cited"]:
        reasons.append(
            f"cited the cause {tools['cause_cited']} times against {alone['cause_cited']}"
        )
    if tools["mrr"] < alone["mrr"]:
        reasons.append(f"ranked it lower (MRR {tools['mrr']} against {alone['mrr']})")
    base = Decimal(alone["cost"])
    if base and Decimal(tools["cost"]) > COST_BAR * base:
        reasons.append(f"cost ${tools['cost']} against ${alone['cost']} (over {COST_BAR}x)")
    if reasons:
        return "Tools do not clear the bar: " + "; ".join(reasons) + "."
    if (tools["cause_cited"], tools["mrr"]) == (alone["cause_cited"], alone["mrr"]):
        return "Tools are no better than one-shot on these cases: leave them off."
    return "Tools clear the bar: switch serviceops_triage_agent_tools on."


def save(record):
    RECORDS.mkdir(parents=True, exist_ok=True)
    stamp = record["generated_at"][:16].replace(":", "")
    model = re.sub(r"[^\w.-]+", "-", record["model"] or "model")
    path = RECORDS / f"{stamp}-{model}.json"
    path.write_text(json.dumps(record, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
