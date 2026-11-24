"""Code documentation's AI step with tools and without, on the application's own code.

    manage.py docs_pack <application id> --as <username> [--repository <name>] [--record]

Whether reading the documented snapshot with tools (`code_docs_agent_tools`) lets
the AI describe more of a repository, and quote more of it, than one call over the
opening lines of a few files per component - and at what cost. **Charges apply**:
the documentation's components are described twice through the application's own
Code documentation configuration, credential and monthly budget, receipted as
usual - one call, then up to `code_docs.TOOL_TURNS` turns with tools. Run
deliberately, never by the suite.

It measures the newest documentation of a repository (`--repository` names which;
by default the newest written here), asked exactly what the button asks
(`code_docs.material`, `INSTRUCTIONS`, the agent's `agent_question`), and judged by
the button's own `judge`: a paragraph is kept only when every quote is word for
word in a file of the component it describes. Scored by rules: components
described, paragraphs and quotes kept, the share of paragraphs that kept their
quotes, how many files they quote, and cost and turns from the receipts each way
wrote. Nothing is stored on the documentation: no result, no step - the receipts
are the only rows it leaves. The documentation's snapshot is the reference (`pack`),
so a newly indexed snapshot starts a new one.
"""

import hashlib
import json
import re
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from django.core.exceptions import PermissionDenied, ValidationError

from .agent_measurements import mark, measuring

ROOT = Path(__file__).resolve().parent.parent
RECORDS = ROOT / "benchmarks" / "docs"
MODES = ("one_shot", "tools")
#: Tools are worth their cost only at no more than this many times one call, and
#: no worse on what is scored - the bar every agent's pack holds.
COST_BAR = 2


def documentation(app, repository=""):
    """The newest documentation of `repository` (or of any) in this application."""
    from .models import CodeDocumentation

    docs = CodeDocumentation.objects.filter(application=app).select_related(
        "snapshot", "repository"
    )
    if repository:
        docs = docs.filter(repository__name=repository)
    doc = docs.order_by("-created_at").first()
    if doc is None or not doc.sections.get("components", {}).get("rows"):
        raise ValidationError(
            "No documentation with components here"
            + (f" for {repository}" if repository else "")
            + ". Write it on a repository's Documentation tab first."
        )
    return doc


def pack_digest(doc):
    from .code_docs import DOCS_VERSION

    return hashlib.sha256(
        json.dumps([doc.repository.name, doc.snapshot.number, doc.snapshot.commit_sha,
                    DOCS_VERSION]).encode()
    ).hexdigest()


def receipts(app, user, since, counted=None):
    """(turns, prompt tokens, completion tokens, amount) the case's receipts recorded:
    since it started, never one an earlier case of this run counted
    (`agent_measurements.case_receipts`)."""
    from .agent_measurements import case_receipts
    from .models import AIUsage

    return case_receipts(
        AIUsage.objects.filter(application=app, actor=user, purpose="code_docs"),
        since,
        set() if counted is None else counted,
    )


def score(result, asked):
    components = result.get("components") or []
    paragraphs = [p for c in components for p in c["paragraphs"]]
    quotes = [q for p in paragraphs for q in p["quotes"]]
    dropped = result.get("dropped", 0)
    return {
        "asked": asked,
        "components": len(components),
        "paragraphs": len(paragraphs),
        "quotes": len(quotes),
        "files_quoted": len({q["source"] for q in quotes}),
        "dropped": dropped,
        "kept_share": round(100 * len(paragraphs) / (len(paragraphs) + dropped), 1)
        if paragraphs or dropped
        else 0.0,
    }


def ask(doc, user, mode):
    """One way: (result fields, failure)."""
    from .code_docs import UNAVAILABLE, describe, material

    citations, owner = material(doc)
    try:
        return describe(doc, user, citations, owner, agent=mode == "tools", steps=False), ""
    except UNAVAILABLE as refused:
        return {}, " ".join(getattr(refused, "messages", [str(refused)]))[:300]


@measuring
def run(user, app, modes=MODES, repository=""):
    """Every way, over the newest documentation. Returns the record `save` writes."""

    from .code_docs import MAX_AI_COMPONENTS, can_write, configured, material
    from .models import AIConfiguration
    from .workbench import access

    _, grant = access(user, app.pk, "code_docs", write=True)
    if not can_write(grant):
        raise PermissionDenied
    if not configured(app.pk):
        raise ValidationError("An owner must set up Code documentation in AI settings first.")
    doc = documentation(app, repository)
    if not material(doc)[0]:
        raise ValidationError("There is no code to describe.")
    asked = len(doc.sections["components"]["rows"][:MAX_AI_COMPONENTS])
    config = AIConfiguration.objects.filter(
        application=app, purpose="code_docs", enabled=True
    ).first()
    details = []
    counted = set()
    for mode in modes:
        since, started = mark(), time.monotonic()
        result, failure = ask(doc, user, mode)
        turns, prompt, completion, amount = receipts(app, user, since, counted)
        details.append({
            "mode": mode,
            **score(result, asked),
            "unreadable": bool(result.get("unreadable")),
            # Why paragraphs were dropped, by the judge's fixed reasons - what a
            # run that kept nothing needs to say. No text of the reply.
            "dropped_reasons": result.get("dropped_reasons") or {},
            "failure": failure,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "turns": turns,
            "tool_calls": result.get("tool_calls", 0),
            "read": result.get("read", 0),
            "cost": str(amount),
            "seconds": round(time.monotonic() - started, 1),
        })
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "provider": getattr(config, "provider", ""),
        "model": getattr(config, "model", ""),
        "pack": pack_digest(doc),
        "snapshot": {"repository": doc.repository.name, "number": doc.snapshot.number},
        "metrics": metrics(details, modes),
        "details": details,
    }


def metrics(details, modes=MODES):
    found = {}
    for mode in modes:
        row = next((item for item in details if item["mode"] == mode), None)
        if row is None:
            continue
        found[mode] = {
            key: row[key]
            for key in ("asked", "components", "paragraphs", "quotes", "files_quoted",
                        "dropped", "kept_share", "turns", "tool_calls", "cost", "seconds")
        }
        found[mode]["failed"] = int(bool(row["failure"]) or row["unreadable"])
    return found


def verdict(found):
    """Whether the tools clear the bar against one call, and why not if they do not."""
    if not {"one_shot", "tools"} <= set(found):
        return "Run both ways to compare."
    from .agent_measurements import nothing_compared, why_nothing_compared

    if nothing_compared(found):
        return why_nothing_compared(found)
    alone, tools = found["one_shot"], found["tools"]
    reasons = []
    if tools["failed"] > alone["failed"]:
        reasons.append("the run with tools failed")
    if tools["components"] < alone["components"]:
        reasons.append(
            f"described {tools['components']} components against {alone['components']}"
        )
    if tools["paragraphs"] < alone["paragraphs"]:
        reasons.append(f"kept {tools['paragraphs']} paragraphs against {alone['paragraphs']}")
    if tools["kept_share"] < alone["kept_share"]:
        reasons.append(
            f"{tools['kept_share']}% kept their quotes against {alone['kept_share']}%"
        )
    base = Decimal(alone["cost"])
    if base and Decimal(tools["cost"]) > COST_BAR * base:
        reasons.append(f"cost ${tools['cost']} against ${alone['cost']} (over {COST_BAR}x)")
    if reasons:
        return "Tools do not clear the bar: " + "; ".join(reasons) + "."
    if (tools["components"], tools["paragraphs"], tools["files_quoted"]) == (
        alone["components"], alone["paragraphs"], alone["files_quoted"]
    ):
        return "Tools are no better than one call on this repository: leave them off."
    return "Tools clear the bar: switch code_docs_agent_tools on."


def save(record):
    RECORDS.mkdir(parents=True, exist_ok=True)
    stamp = record["generated_at"][:16].replace(":", "")
    model = re.sub(r"[^\w.-]+", "-", record["model"] or "model")
    from .agent_measurements import unused

    path = unused(RECORDS / f"{stamp}-{model}.json")
    path.write_text(json.dumps(record, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
