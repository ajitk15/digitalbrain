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

from .agent_measurements import mark, measuring

ROOT = Path(__file__).resolve().parent.parent
RECORDS = ROOT / "benchmarks" / "triage"
#: Labelled CarePath incidents and changes, for an application with no causes a
#: person confirmed (Phase D of `agentic-ai-next-plan.md`): each open incident
#: names the record that is its cause.
PACK = ROOT / "benchmarks" / "triage-pack.json"
MODES = ("one_shot", "tools")
#: The header lines a ServiceNow import writes for each table, in its order
#: (`connector_kinds.servicenow_records`), so a loaded record is stored exactly as
#: an imported one and the operations layer reads it the same way.
HEADERS = {
    "incident": (
        "Incident",
        ("Number", "State", "Priority", "Impact", "Service", "CI", "Environment",
         "Assignment group", "Opened", "Resolved", "Close code", "Close notes", "Problem",
         "Caused by change"),
    ),
    "change_request": (
        "Change", ("Number", "State", "Service", "CI", "Start", "End", "Change type")
    ),
}


def load_pack():
    return json.loads(PACK.read_text(encoding="utf-8"))


def pack_digest(pack):
    import hashlib

    return hashlib.sha256(json.dumps(pack, sort_keys=True).encode()).hexdigest()


def record_source(pack, record):
    """The record's address, as the ServiceNow connector builds it."""
    from urllib.parse import quote

    return (
        f"{pack['base_url']}/nav_to.do?uri={quote(record['table'])}.do%3Fsys_id%3D"
        f"{quote(record['sys_id'])}"
    )


def record_content(record):
    """The record's stored text: its title, its header, its text - as `sync` writes it."""
    from .connector_kinds import incident_header

    kind, labels = HEADERS[record["table"]]
    fields = record.get("fields") or {}
    pairs = [("Type", kind)] + [(label, fields.get(label, "")) for label in labels]
    header = incident_header(pairs)
    body = f"{header}\n\n{record.get('text', '')}".strip()
    return f"{record['title']}\n\n{body}"


def load_incidents(user, app, pack=None):
    """Add the pack's incidents and changes to `app` once, as an import would.

    Returns (added, unchanged). A record already held unchanged at its address is
    left alone; one the pack changed is retired and added again, as `sync` does,
    so loading twice adds nothing. Each is an ordinary source, through
    `add_knowledge` - masking, the personal-data scan, a graph refresh - so adding
    sources may start the application's automatic AI build, which is paid.
    """
    import hashlib

    from .masking import stored_content
    from .models import KnowledgeEntry
    from .workbench import access, add_knowledge

    pack = pack or load_pack()
    access(user, app.pk, "service_ops", write=True)
    added = unchanged = 0
    for record in pack["records"]:
        source = record_source(pack, record)
        content = record_content(record)
        digest = hashlib.sha256(stored_content(app.pk, content).encode()).hexdigest()
        existing = KnowledgeEntry.objects.filter(application=app, source=source, active=True)
        if existing.filter(digest=digest).exists():
            unchanged += 1
            continue
        existing.update(active=False)
        add_knowledge(user, app.pk, record["title"], content, source=source)
        added += 1
    return added, unchanged


def labelled_cases(app, pack=None):
    """[(incident, cause)] for each of the pack's cases whose incident and labelled
    cause are both loaded here, answerable and unchanged since they were stored."""
    from .models import KnowledgeEntry
    from .serviceops import verified

    pack = pack or load_pack()
    by_number = {record["fields"]["Number"]: record for record in pack["records"]}

    def held(number):
        record = by_number.get(number)
        if record is None:
            return None
        entry = (
            KnowledgeEntry.objects.filter(application=app, source=record_source(pack, record))
            .answerable()
            .first()
        )
        return entry if entry is not None and verified(entry) else None

    found = []
    for number, label in pack["cases"].items():
        incident, cause = held(number), held(label["cause"])
        if incident is not None and cause is not None:
            found.append((incident, cause))
    return found
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


def ask_case(user, app, incident, cause, mode, config, counted=None):

    from .agent_measurements import case_receipts
    from .models import AIUsage
    from .serviceops_triage import _parsed_hypotheses, ask_model, evidence_pack

    pack, _ = evidence_pack(app, incident, own_confirmed=False)
    receipt, failure, answer, extra = {}, "", "", []
    started, since = time.monotonic(), mark()
    try:
        answer, extra = ask_model(user, app, incident, pack, receipt, agent=mode == "tools")
    except (ValidationError, ImproperlyConfigured, OSError, TimeoutError) as refused:
        failure = " ".join(getattr(refused, "messages", [str(refused)]))[:300]
    hypotheses = _parsed_hypotheses(answer, pack + extra) if answer else []
    cause_id = str(cause.pk)
    rank = score(hypotheses, cause_id)
    # What the case cost is what its receipts say - the bill, cache reads and
    # writes priced apart - never tokens priced here: the CLI writes every one-call
    # prompt to the hour's cache at twice the input rate and a tool run reads most
    # of its prompt back at a tenth, so pricing tokens read one call at about half
    # its bill and tools at about half again over theirs. A run that failed after
    # it was billed is counted too.
    turns, prompt, completion, spent = case_receipts(
        AIUsage.objects.filter(application=app, actor=user, purpose="serviceops_triage"),
        since,
        set() if counted is None else counted,
    )
    return {
        "incident": str(incident.pk),
        "title": incident.title[:120],
        "mode": mode,
        "rank": rank,
        "hypotheses": len(hypotheses),
        "cause_in_pack": any(row["id"] == cause_id for row in pack),
        "cause_read": any(row["id"] == cause_id for row in extra),
        "failure": failure,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "turns": turns,
        "tool_calls": receipt.get("tool_calls", 0),
        "cost": str(spent),
        "seconds": round(time.monotonic() - started, 1),
    }


@measuring
def run(user, app, modes=MODES, limit=MAX_CASES, labelled=False):
    """Every case every way. Returns the record `save` writes.

    `labelled` scores against `PACK`'s labels instead of the causes people
    confirmed: the pack's digest is then the record's `pack`, so a labelled run is
    a different reference from one against verdicts."""
    from .models import AIConfiguration

    pack = load_pack() if labelled else None
    found = labelled_cases(app, pack)[:limit] if labelled else cases(app, limit)
    if not found and labelled:
        raise ValidationError(
            "The labelled incidents are not here: load them with --load-incidents first."
        )
    if not found:
        raise ValidationError(
            "No incident here has a cause people confirmed. Confirm causes on ServiceOps "
            "first - the pack measures triage against them - or load the labelled "
            "incidents with --load-incidents and run with --labelled."
        )
    config = AIConfiguration.objects.filter(
        application=app, purpose="serviceops_triage", enabled=True
    ).first()
    counted = set()
    details = [
        ask_case(user, app, incident, cause, mode, config, counted)
        for incident, cause in found
        for mode in modes
    ]
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "provider": getattr(config, "provider", ""),
        "model": getattr(config, "model", ""),
        "labels": "pack" if labelled else "verdicts",
        **({"pack": pack_digest(pack)} if labelled else {}),
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
    from .agent_measurements import nothing_compared, why_nothing_compared

    if nothing_compared(found):
        return why_nothing_compared(found)
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
    from .agent_measurements import unused

    path = unused(RECORDS / f"{stamp}-{model}.json")
    path.write_text(json.dumps(record, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
