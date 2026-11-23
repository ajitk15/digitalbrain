"""A document's AI review with tools and without, on contradictions planted in CarePath.

    manage.py document_review_pack <application id> --as <username> [--record]

Whether letting the review search the application's other sources
(`document_review_agent_tools`) finds where a document contradicts them more often
than one call over the document alone - and at what cost. **Charges apply**: each
item of `benchmarks/document-review-pack.json` is reviewed twice through the
application's own Document review configuration, credential and monthly budget,
receipted as usual - one call, then up to `document_ai.TOOL_TURNS` turns with tools.
Run deliberately, never by the suite.

An item names a CarePath document by its upload name, a line of it and what to
put in its place: a value another CarePath document contradicts (the retention of
audit events, a Sev-1 page time, a token's rotation). The line is replaced **in
memory only** - the stored source is never changed, and nothing is written but the
receipts - and the edited text is reviewed exactly as the button reviews a source
(`document_ai.review_answer`, its instructions, its judge), as the type the rules
give the stored document. Each label is tested against the file under
`documentation/demo/docs/`, so it cannot describe a line that is not there.

Scored by rules, from the findings `judge_review` keeps: **found** when a kept
contradiction finding quotes the planted text; **linked** when that finding's other
side - quoted, exactly, from a source the review read - is a source the item names.
One call reads no other source, so it can find a planted line only from the document
itself and can never link one.
"""

import hashlib
import json
import re
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from django.core.exceptions import ValidationError

ROOT = Path(__file__).resolve().parent.parent
PACK = ROOT / "benchmarks" / "document-review-pack.json"
RECORDS = ROOT / "benchmarks" / "document-review"
DOCS = ROOT / "documentation" / "demo" / "docs"
MODES = ("one_shot", "tools")
#: Tools are worth their cost only at no more than this many times one call, and
#: no worse on what is scored - the bar every agent's pack holds.
COST_BAR = 2


def load_pack():
    if not PACK.is_file():
        raise ValidationError(
            "benchmarks/document-review-pack.json is missing. It is frozen: restore it from "
            "the repository rather than writing a new one, which would start a new reference."
        )
    return json.loads(PACK.read_text(encoding="utf-8"))


def pack_digest(pack):
    return hashlib.sha256(json.dumps(pack, sort_keys=True).encode()).hexdigest()


def source_for(app, name):
    """The answerable source uploaded as `name`: its exact title first, then one
    whose title or origin ends with it."""
    from .models import KnowledgeEntry

    found = KnowledgeEntry.objects.answerable().filter(application=app)
    return (
        found.filter(title__iexact=name).first()
        or found.filter(title__iendswith=name).first()
        or found.filter(source__iendswith=name).first()
    )


def score(findings, item):
    """found: a kept contradiction quotes the planted text. linked: its other side
    is a source the item names."""
    from .benchmark import matches

    planted = item["planted"]
    hits = [
        f for f in findings
        if f["aspect"] == "contradiction" and (planted in f["quote"] or f["quote"] in planted)
    ]
    linked = any(
        f.get("other") and any(
            matches(SimpleNamespace(title=f["other"]["title"], source=""), name)
            for name in item["others"]
        )
        for f in hits
    )
    return {"found": bool(hits), "linked": linked}


def receipts(app, user, since):
    from django.db.models import Count, Sum

    from .models import AIUsage

    found = AIUsage.objects.filter(
        application=app, actor=user, purpose="document_review", created_at__gte=since
    ).aggregate(
        turns=Count("id"), prompt=Sum("input_tokens"), completion=Sum("output_tokens"),
        amount=Sum("amount"),
    )
    return (found["turns"], found["prompt"] or 0, found["completion"] or 0,
            found["amount"] or Decimal("0"))


def ask_item(user, app, item, mode):
    """One item one way: (findings, kept, dropped, failure)."""
    from .document_ai import UNAVAILABLE, current_kind, review_answer

    entry = source_for(app, item["document"])
    if entry is None:
        return [], 0, 0, f"No source here is {item['document']}."
    if item["find"] not in entry.content:
        return [], 0, 0, f"{item['document']} here does not hold the labelled line."
    subject = SimpleNamespace(
        pk=entry.pk,
        application_id=app.pk,
        title=entry.title,
        content=entry.content.replace(item["find"], item["replace"], 1),
    )
    try:
        result = review_answer(user, subject, current_kind(entry), agent=mode == "tools",
                               steps=False)
    except UNAVAILABLE as refused:
        return [], 0, 0, " ".join(getattr(refused, "messages", [str(refused)]))[:300]
    return result["findings"], len(result["findings"]), result["dropped"], ""


def run(user, app, modes=MODES):
    """Every item every way. Returns the record `save` writes."""
    from django.utils import timezone

    from .document_ai import configured
    from .models import AIConfiguration
    from .workbench import access

    access(user, app.pk, "knowledge")
    if not configured(app.pk, "document_review"):
        raise ValidationError("An owner must set up Document review in AI settings first.")
    pack = load_pack()
    config = AIConfiguration.objects.filter(
        application=app, purpose="document_review", enabled=True
    ).first()
    details = []
    for key, item in pack["items"].items():
        for mode in modes:
            since, started = timezone.now(), time.monotonic()
            findings, kept, dropped, failure = ask_item(user, app, item, mode)
            turns, prompt, completion, amount = receipts(app, user, since)
            details.append({
                "item": key,
                "mode": mode,
                **score(findings, item),
                "findings": kept,
                "dropped": dropped,
                "failure": failure,
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "turns": turns,
                "cost": str(amount),
                "seconds": round(time.monotonic() - started, 1),
            })
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "provider": getattr(config, "provider", ""),
        "model": getattr(config, "model", ""),
        "pack": pack_digest(pack),
        "metrics": metrics(details, modes),
        "details": details,
    }


def metrics(details, modes=MODES):
    found = {}
    for mode in modes:
        rows = [row for row in details if row["mode"] == mode]
        found[mode] = {
            "items": len(rows),
            "found": sum(row["found"] for row in rows),
            "linked": sum(row["linked"] for row in rows),
            "findings": sum(row["findings"] for row in rows),
            "dropped": sum(row["dropped"] for row in rows),
            "failed": sum(bool(row["failure"]) for row in rows),
            "cost": str(sum((Decimal(row["cost"]) for row in rows), Decimal("0"))),
            "turns": sum(row["turns"] for row in rows),
            "seconds": round(sum(row["seconds"] for row in rows), 1),
        }
    return found


def verdict(found):
    """Whether the tools clear the bar against one call, and why not if they do not."""
    if not {"one_shot", "tools"} <= set(found):
        return "Run both ways to compare."
    from .agent_measurements import NOTHING_COMPARED, nothing_compared

    if nothing_compared(found):
        return NOTHING_COMPARED
    alone, tools = found["one_shot"], found["tools"]
    reasons = []
    if tools["failed"] > alone["failed"]:
        reasons.append(f"{tools['failed']} items unanswered against {alone['failed']}")
    if tools["found"] < alone["found"]:
        reasons.append(f"found {tools['found']} planted contradictions against {alone['found']}")
    base = Decimal(alone["cost"])
    if base and Decimal(tools["cost"]) > COST_BAR * base:
        reasons.append(f"cost ${tools['cost']} against ${alone['cost']} (over {COST_BAR}x)")
    if reasons:
        return "Tools do not clear the bar: " + "; ".join(reasons) + "."
    if (tools["found"], tools["linked"]) == (alone["found"], alone["linked"]):
        return "Tools are no better than one call on this pack: leave them off."
    return "Tools clear the bar: switch document_review_agent_tools on."


def save(record):
    RECORDS.mkdir(parents=True, exist_ok=True)
    stamp = record["generated_at"][:16].replace(":", "")
    model = re.sub(r"[^\w.-]+", "-", record["model"] or "model")
    path = RECORDS / f"{stamp}-{model}.json"
    path.write_text(json.dumps(record, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
