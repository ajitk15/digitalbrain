"""Code Factory's gap analysis with tools and without, on CarePath's planted bugs.

    manage.py analysis_pack <application id> --as <username> [--record]

Whether letting gap analysis search the application's sources and read the code
(`gap_analysis_agent_tools`) makes a plan rest on more of the evidence that
reveals a ticket's bug than one call over what retrieval chose - and at what cost.
**Charges apply**: each ticket of `benchmarks/seeded-evidence.json` is analysed
twice through the application's own Code Factory configuration, credential and
monthly budget, receipted as usual - one call, then up to
`code_factory_analysis.ANALYSIS_TOOL_TURNS` turns with tools. Run deliberately,
never by the suite.

The answer key is the scorecard's: for each CarePath ticket with a planted bug,
the identifiers in the project's own documents that reveal it (BR-05, C-08,
R-02...), from `documentation/demo/demo-kit/seeded-gaps.md`. The scorecard counts
how many are in the evidence analysis is *handed*, free and by rule; this counts
how many the plan's items *cite* - what a reviewer is shown an item rests on -
which only a model can change.

Each ticket is asked as a run asks it - `evidence_question`, `gather_evidence`,
`analyse`, `collect_items` - against the published knowledge and, where one holds
the planted files, the application's CarePath code snapshot. Triage is not asked:
its reading of the ticket is a model's, so the question is the ticket with an
empty triage, as the scorecard asks it, and both ways get the same one. Nothing is
written but the receipts: no run, plan or step.

Scored by rules: an identifier counts as cited when it appears, whole, in the
title or excerpt of a citation an item kept; and as offered when it appears in the
evidence the analysis was handed. Items, items with evidence, citations refused,
cost and turns are counted too.
"""

import hashlib
import json
import re
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from django.core.exceptions import ImproperlyConfigured, ValidationError

ROOT = Path(__file__).resolve().parent.parent
KEY = ROOT / "benchmarks" / "seeded-evidence.json"
CODE_KEY = ROOT / "benchmarks" / "code-evidence.json"
RECORDS = ROOT / "benchmarks" / "analysis"
MODES = ("one_shot", "tools")
#: Tools are worth their cost only at no more than this many times one call, and
#: no worse on what is scored - the bar every agent's pack holds.
COST_BAR = 2


def load_key():
    if not KEY.is_file():
        raise ValidationError(
            "benchmarks/seeded-evidence.json is missing. It is the scorecard's answer key: "
            "restore it from the repository."
        )
    return json.loads(KEY.read_text(encoding="utf-8"))["tickets"]


def pack_digest(key):
    return hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()


def named(identifier, text):
    """Whether `identifier` appears whole in `text` - C-08, never C-080 or AC-08."""
    return re.search(rf"(?<![\w-]){re.escape(identifier)}(?![\w-])", text or "") is not None


def text_of(citations):
    return "\n".join(f"{c.get('title', '')}\n{c.get('excerpt', '')}" for c in citations)


def ticket_for(app, care):
    """The application's imported ticket for a CarePath key: its title starts with it."""
    from .models import KnowledgeEntry

    return (
        KnowledgeEntry.objects.answerable()
        .filter(application=app, title__startswith=care)
        .order_by("-created_at")
        .first()
    )


def snapshot_for(app):
    """The newest code snapshot of this application holding every planted file, or None."""
    from .models import CodeSnapshot

    wanted = {
        path
        for places in json.loads(CODE_KEY.read_text(encoding="utf-8"))["tickets"].values()
        for path, _ in places
    }
    for snapshot in CodeSnapshot.objects.filter(
        repository__application=app, repository__retired_at__isnull=True
    ).order_by("-created_at"):
        held = set(snapshot.files.filter(path__in=wanted).values_list("path", flat=True))
        if held >= wanted:
            return snapshot
    return None


def score(items, offered, found, identifiers):
    cited = [c for item in items for c in item["citations"]]
    return {
        "expected": len(identifiers),
        "cited": sum(named(i, text_of(cited)) for i in identifiers),
        "offered": sum(named(i, text_of(offered)) for i in identifiers),
        "found_more": sum(
            named(i, text_of(found)) and not named(i, text_of(offered)) for i in identifiers
        ),
        "items": len(items),
        "evidenced": sum(bool(item["citations"]) for item in items),
    }


def ask_ticket(user, app, entry, version, snapshot, mode):
    """One ticket one way: (items, offered, found, rejected, failure)."""
    from .code_factory import MANUAL_TICKET_LIMIT, ticket_key
    from .code_factory_analysis import (
        analyse,
        collect_items,
        evidence_question,
        gather_evidence,
    )

    run = SimpleNamespace(
        application_id=app.pk,
        application=app,
        requested_by=user,
        code_snapshot_id=snapshot.pk if snapshot else None,
        code_snapshot=snapshot,
        graph_version=version,
        ticket_url=entry.source,
        ticket_body="",
        ticket_external_id=ticket_key(entry),
        ticket_title=entry.title[:300],
    )
    triage = {"summary": "", "requirements": []}
    try:
        question = evidence_question(run, triage, entry.content[:MANUAL_TICKET_LIMIT])
        question, graph, extra = gather_evidence(run, question)
        offered = graph + extra
        payload, found = analyse(
            user, app.pk, question, offered, {}, snapshot_id=run.code_snapshot_id,
            agent=mode == "tools",
        )
        items, _, rejected = collect_items(run, payload, offered, found)
    except (ValidationError, ImproperlyConfigured) as refused:
        return [], [], [], 0, " ".join(getattr(refused, "messages", [str(refused)]))[:300]
    unique = list({id(c): c for c in found.values()}.values())
    return items, offered, unique, rejected, ""


def receipts(app, user, since):
    from django.db.models import Count, Sum

    from .models import AIUsage

    totals = AIUsage.objects.filter(
        application=app, actor=user, purpose="plan_drafting", created_at__gte=since
    ).aggregate(
        turns=Count("id"), prompt=Sum("input_tokens"), completion=Sum("output_tokens"),
        amount=Sum("amount"),
    )
    return (totals["turns"], totals["prompt"] or 0, totals["completion"] or 0,
            totals["amount"] or Decimal("0"))


def run(user, app, modes=MODES):
    """Every ticket every way. Returns the record `save` writes."""
    from django.utils import timezone

    from .code_factory import NO_GRAPH
    from .graphs import published_revision
    from .models import AIConfiguration
    from .workbench import access

    access(user, app.pk, "code_factory", write=True)
    config = AIConfiguration.objects.filter(
        application=app, purpose="plan_drafting", enabled=True
    ).first()
    if config is None:
        raise ValidationError("An owner must set up Code Factory in AI settings first.")
    published = published_revision(app.pk)
    if published is None:
        raise ValidationError(NO_GRAPH)
    version = published.number
    key = load_key()
    snapshot = snapshot_for(app)
    details = []
    for care, identifiers in key.items():
        entry = ticket_for(app, care)
        for mode in modes:
            if entry is None:
                details.append({"ticket": care, "mode": mode, **score([], [], [], identifiers),
                                "failure": f"No imported ticket here is titled {care}.",
                                "rejected": 0, "prompt_tokens": 0, "completion_tokens": 0,
                                "turns": 0, "cost": "0", "seconds": 0.0})
                continue
            since, started = timezone.now(), time.monotonic()
            items, offered, found, rejected, failure = ask_ticket(
                user, app, entry, version, snapshot, mode
            )
            turns, prompt, completion, amount = receipts(app, user, since)
            details.append({
                "ticket": care,
                "mode": mode,
                **score(items, offered, found, identifiers),
                "rejected": rejected,
                "failure": failure,
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "turns": turns,
                "cost": str(amount),
                "seconds": round(time.monotonic() - started, 1),
            })
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "provider": config.provider,
        "model": config.model,
        "pack": pack_digest(key),
        "code": snapshot is not None,
        "metrics": metrics(details, modes),
        "details": details,
    }


def metrics(details, modes=MODES):
    found = {}
    for mode in modes:
        rows = [row for row in details if row["mode"] == mode]
        expected = sum(row["expected"] for row in rows)
        cited = sum(row["cited"] for row in rows)
        found[mode] = {
            "tickets": len(rows),
            "expected": expected,
            "cited": cited,
            "cited_share": round(100 * cited / expected, 1) if expected else 0.0,
            "offered": sum(row["offered"] for row in rows),
            "found_more": sum(row["found_more"] for row in rows),
            "items": sum(row["items"] for row in rows),
            "evidenced": sum(row["evidenced"] for row in rows),
            "rejected": sum(row["rejected"] for row in rows),
            "failed": sum(bool(row["failure"]) for row in rows),
            "cost": str(sum((Decimal(row["cost"]) for row in rows), Decimal("0"))),
            "turns": sum(row["turns"] for row in rows),
            "seconds": round(sum(row["seconds"] for row in rows), 1),
        }
    return found


def verdict(found):
    """Whether gap analysis's tools clear the bar against one call, and why not."""
    if not {"one_shot", "tools"} <= set(found):
        return "Run both ways to compare."
    from .agent_measurements import NOTHING_COMPARED, nothing_compared

    if nothing_compared(found):
        return NOTHING_COMPARED
    alone, tools = found["one_shot"], found["tools"]
    reasons = []
    if tools["failed"] > alone["failed"]:
        reasons.append(f"{tools['failed']} tickets unanswered against {alone['failed']}")
    if tools["cited"] < alone["cited"]:
        reasons.append(f"cited {tools['cited']} planted identifiers against {alone['cited']}")
    if tools["evidenced"] < alone["evidenced"]:
        reasons.append(f"{tools['evidenced']} items with evidence against {alone['evidenced']}")
    base = Decimal(alone["cost"])
    if base and Decimal(tools["cost"]) > COST_BAR * base:
        reasons.append(f"cost ${tools['cost']} against ${alone['cost']} (over {COST_BAR}x)")
    if reasons:
        return "Tools do not clear the bar: " + "; ".join(reasons) + "."
    if tools["cited"] == alone["cited"]:
        return "Tools are no better than one call on this pack: leave them off."
    return "Tools clear the bar: switch gap_analysis_agent_tools on."


def save(record):
    RECORDS.mkdir(parents=True, exist_ok=True)
    stamp = record["generated_at"][:16].replace(":", "")
    model = re.sub(r"[^\w.-]+", "-", record["model"] or "model")
    path = RECORDS / f"{stamp}-{model}.json"
    path.write_text(json.dumps(record, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
