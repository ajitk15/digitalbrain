"""A code review's AI step with tools and without, on labelled CarePath changes.

    manage.py review_pack <application id> --as <username> [--record] [--only tools]

Phase 4 of `documentation/engineering/agentic-ai-plan.md`: whether letting the
review model read the base snapshot, whole diffs and the published requirements
(`code_review_agent_tools`) finds problems that only show outside the excerpts it
is handed - a caller left behind by a rename, a requirement a change breaks - and
at what cost. **Charges apply**: each change of `benchmarks/review-pack.json` is
reviewed twice through the application's own Code review configuration, credential
and monthly budget, receipted as usual - one call one-shot, up to
`code_review.TOOL_TURNS` with tools. Run deliberately, never by the suite.

Each change is applied to the application's newest snapshot holding every file the
pack names (index `documentation/demo/carepath` on Code Repos) as a review would
read it - `unified` diffs, `summarise` - in a review row of its own, asked exactly
what a review asks (`material`, `INSTRUCTIONS`, `judge` over `live_texts`), and the
row removed afterwards with its files: nothing is left but the receipts. Scored by
rules from the kept findings: a change is found when a finding quotes one of the
files `expect` names (its diff or its code), or is of the category it names; every
kept finding that does neither is counted as noise.
"""

import hashlib
import json
import re
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured, ValidationError

ROOT = Path(__file__).resolve().parent.parent
PACK = ROOT / "benchmarks" / "review-pack.json"
RECORDS = ROOT / "benchmarks" / "review"
MODES = ("one_shot", "tools")
#: Phase 1's bar: tools only at no more than this many times the one-shot cost.
COST_BAR = 2


def load_pack():
    if not PACK.is_file():
        raise ValidationError(
            "benchmarks/review-pack.json is missing. It is frozen: restore it from the "
            "repository rather than writing a new one, which would start a new reference."
        )
    return json.loads(PACK.read_text(encoding="utf-8"))


def pack_paths(pack):
    return {
        path
        for change in pack["changes"].values()
        for path in [edit["path"] for edit in change["edits"]]
        + change["expect"].get("paths", [])
    }


def snapshot_for(app, pack):
    from .models import CodeSnapshot

    wanted = pack_paths(pack)
    for snapshot in CodeSnapshot.objects.filter(
        repository__application=app, repository__retired_at__isnull=True
    ).order_by("-created_at"):
        held = set(snapshot.files.filter(path__in=wanted).values_list("path", flat=True))
        if held >= wanted:
            return snapshot
    raise ValidationError(
        "No code snapshot of this application holds every file the pack names. Index "
        f"{pack['repository']} on Knowledge → Code Repos first."
    )


def apply(content, edit):
    """The file after one edit, or None when its text is not there exactly once."""
    if content.count(edit["find"]) != 1:
        return None
    return content.replace(edit["find"], edit["replace"], 1)


def build(user, app, snapshot, change):
    """A review row of the change, read as `read_change` reads one. The caller removes it."""
    from .code_review import digest_of, summarise, unified
    from .models import CodeReview, CodeReviewFile

    review = CodeReview.objects.create(
        application=app,
        repository=snapshot.repository,
        snapshot=snapshot,
        kind="branch",
        ref="review-pack",
        requested_by=user,
        status="ready",
    )
    rows = []
    for edit in change["edits"]:
        before = snapshot.files.get(path=edit["path"]).content
        after = apply(before, edit)
        if after is None:
            raise ValidationError(f"The pack's edit to {edit['path']} no longer applies.")
        diff, added, removed = unified(edit["path"], before, after)
        rows.append(
            CodeReviewFile(
                review=review,
                path=edit["path"],
                change="modified",
                language="python",
                base_digest=digest_of(before),
                head_digest=digest_of(after),
                head_content=after,
                diff=diff,
                added=added,
                removed=removed,
            )
        )
    CodeReviewFile.objects.bulk_create(rows)
    CodeReview.objects.filter(pk=review.pk).update(summary=summarise(review, rows))
    review.refresh_from_db()
    return review


def score(change, findings, snapshot):
    """(found, noise): whether a kept finding meets `expect`, and how many do not."""
    expect = change["expect"]
    paths = set(expect.get("paths", []))
    code_ids = {
        f"code:{pk}"
        for pk in snapshot.files.filter(path__in=paths).values_list("pk", flat=True)
    }
    wanted = code_ids | {f"diff:{path}" for path in paths}

    def hits(finding):
        if expect.get("category"):
            return finding["category"] == expect["category"]
        return any(quote["source"] in wanted for quote in finding["quotes"])

    met = [finding for finding in findings if hits(finding)]
    return bool(met), len(findings) - len(met)


def review_change(user, app, snapshot, key, change, mode, config):
    from .ai import cost
    from .code_review import material, review_answer

    started = time.monotonic()
    review = build(user, app, snapshot, change)
    failure, result = "", {}
    try:
        citations, _ = material(review, user)
        result = review_answer(review, user, citations, agent=mode == "tools", steps=False)
    except (ValidationError, ImproperlyConfigured, OSError, TimeoutError) as refused:
        failure = " ".join(getattr(refused, "messages", [str(refused)]))[:300]
    finally:
        review.delete()
    findings = result.get("findings", [])
    found, noise = score(change, findings, snapshot) if not failure else (False, 0)
    spent = (
        cost(config, result.get("prompt_tokens", 0), result.get("completion_tokens", 0))
        if config and result
        else 0
    )
    return {
        "change": key,
        "mode": mode,
        "found": found,
        "findings": len(findings),
        "noise": noise,
        "dropped": result.get("dropped", 0),
        "read": result.get("read", 0),
        "failure": failure,
        "turns": result.get("turns", 0),
        "tool_calls": result.get("tool_calls", 0),
        "cost": str(spent),
        "seconds": round(time.monotonic() - started, 1),
    }


def run(user, app, modes=MODES):
    from .models import AIConfiguration

    pack = load_pack()
    snapshot = snapshot_for(app, pack)
    config = AIConfiguration.objects.filter(
        application=app, purpose="code_review", enabled=True
    ).first()
    details = [
        review_change(user, app, snapshot, key, change, mode, config)
        for key, change in pack["changes"].items()
        for mode in modes
    ]
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "provider": getattr(config, "provider", ""),
        "model": getattr(config, "model", ""),
        "pack": hashlib.sha256(json.dumps(pack, sort_keys=True).encode()).hexdigest(),
        "snapshot": {"repository": snapshot.repository.name, "number": snapshot.number},
        "metrics": metrics(details, modes),
        "details": details,
    }


def metrics(details, modes=MODES):
    found = {}
    for mode in modes:
        rows = [row for row in details if row["mode"] == mode]
        found[mode] = {
            "changes": len(rows),
            "found": sum(row["found"] for row in rows),
            "noise": sum(row["noise"] for row in rows),
            "failed": sum(bool(row["failure"]) for row in rows),
            "cost": str(sum((Decimal(row["cost"]) for row in rows), Decimal("0"))),
            "turns": sum(row["turns"] for row in rows),
            "tool_calls": sum(row["tool_calls"] for row in rows),
            "seconds": round(sum(row["seconds"] for row in rows), 1),
        }
    return found


def verdict(found):
    if not {"one_shot", "tools"} <= set(found):
        return "Run both ways to compare."
    from .agent_measurements import NOTHING_COMPARED, nothing_compared

    if nothing_compared(found):
        return NOTHING_COMPARED
    alone, tools = found["one_shot"], found["tools"]
    reasons = []
    if tools["found"] < alone["found"]:
        reasons.append(f"found {tools['found']} changes against {alone['found']}")
    if tools["noise"] > alone["noise"]:
        reasons.append(f"{tools['noise']} findings that are noise against {alone['noise']}")
    base = Decimal(alone["cost"])
    if base and Decimal(tools["cost"]) > COST_BAR * base:
        reasons.append(f"cost ${tools['cost']} against ${alone['cost']} (over {COST_BAR}x)")
    if reasons:
        return "Tools do not clear the bar: " + "; ".join(reasons) + "."
    if tools["found"] == alone["found"]:
        return "Tools are no better than one-shot on this pack: leave them off."
    return "Tools clear the bar: switch code_review_agent_tools on."


def save(record):
    RECORDS.mkdir(parents=True, exist_ok=True)
    stamp = record["generated_at"][:16].replace(":", "")
    model = re.sub(r"[^\w.-]+", "-", record["model"] or "model")
    path = RECORDS / f"{stamp}-{model}.json"
    path.write_text(json.dumps(record, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
