"""Code Factory's implementation agent with tools and without, on a frozen pack.

    manage.py factory_pack <application id> --as <username> [--record]

Phase 1 of `documentation/engineering/agentic-ai-plan.md`: whether reading the
snapshot with tools (`agent_runtime/code_tools.py`) makes a better change than
the one-shot call over files chosen beforehand, and at what cost. **Charges
apply**: each ticket of `benchmarks/factory-pack.json` is sent twice, once each
way, through the application's own Code Factory configuration, credential and
monthly budget, receipted as usual - one call one-shot, up to
`code_factory_agents.TOOL_TURNS` with tools. Like `llm_drift` and `typing_pack`,
it is run deliberately and never by the test suite.

Each ticket is asked exactly what a run asks - `implementation_question` over the
same `reference_files`, `numbered_files` and instructions - against a snapshot of
the application that holds every file the pack names (index
`documentation/demo/carepath` on Code Repos). Nothing is written: no run, no
plan, no change; the receipts are the only rows it leaves.

What is scored, by rules, from the answer: whether it parsed and changed a
target (`collect_changes`, the run's own rule), each labelled `expect` (text the
fixed symbol must contain) and `forbid` (text it must no longer contain), and
whether the files a correct fix depends on were in front of the model - in its
reference for both ways, and also read with `read_file` with tools. These are a
proxy for a correct fix, not proof; CarePath's own tests are the proof.
"""

import hashlib
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from django.core.exceptions import ValidationError

ROOT = Path(__file__).resolve().parent.parent
PACK = ROOT / "benchmarks" / "factory-pack.json"
RECORDS = ROOT / "benchmarks" / "factory"
MODES = ("one_shot", "tools")
#: The plan's bar: tools are worth turning on only at no more than this many
#: times the one-shot cost, and no worse on what is scored.
COST_BAR = 2


def load_pack():
    if not PACK.is_file():
        raise ValidationError(
            "benchmarks/factory-pack.json is missing. It is frozen: restore it from the "
            "repository rather than writing a new one, which would start a new reference."
        )
    return json.loads(PACK.read_text(encoding="utf-8"))


def pack_digest(pack):
    return hashlib.sha256(json.dumps(pack, sort_keys=True).encode()).hexdigest()


def pack_paths(pack):
    return {
        path
        for item in pack["tickets"].values()
        for path in [*item["targets"], *item["needs"]]
    }


def snapshot_for(app, pack):
    """The newest snapshot of this application that holds every file the pack names."""
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


def region(content, symbol):
    """The text of one definition, or None when it is gone or the file does not parse."""
    from .scorecard import symbol_span

    try:
        span = symbol_span(content, symbol)
    except (SyntaxError, ValueError):
        return None
    if span is None:
        return None
    lines = content.splitlines()
    return "\n".join(lines[span[0] - 1 : span[1]])


def score(item, originals, answer):
    """What one answer did, by rules: parsed, changed, each expect and forbid met."""
    from .code_factory_agents import collect_changes
    from .code_factory_analysis import parse_json

    files = [{"path": path, "text": originals[path], "sha": None} for path in item["targets"]]
    found = {"answered": False, "changed": [], "expect": [], "forbid": []}
    try:
        payload = parse_json(answer, "Implementation")
        changes = collect_changes(payload, files)
    except ValidationError:
        changes = []
    else:
        found["answered"] = True
    written = {change["path"]: change["content"] for change in changes}
    found["changed"] = sorted(written)
    for kind, wanted in (("expect", True), ("forbid", False)):
        for path, symbol, text in item[kind]:
            body = region(written[path], symbol) if path in written else None
            met = body is not None and ((text in body) is wanted)
            found[kind].append({"path": path, "symbol": symbol, "text": text, "met": met})
    found["fixed"] = bool(written) and all(
        check["met"] for check in found["expect"] + found["forbid"]
    )
    return found


def ask_ticket(user, app, snapshot, item, mode):
    """One ticket, one way: (answer, receipt, files read with tools, references)."""
    from .code_factory_agents import (
        IMPLEMENTATION_INSTRUCTIONS,
        agent_answer,
        implementation_question,
        numbered_files,
        reference_files,
    )
    from .code_factory_analysis import ask

    held = {
        found.path: found.content
        for found in snapshot.files.filter(path__in=item["targets"])
    }
    files = [{"path": path, "text": held[path], "sha": None} for path in item["targets"]]
    summary = f"{item['title']}\n{item['change']}"
    run = SimpleNamespace(code_snapshot_id=snapshot.pk, code_snapshot=snapshot)
    references = reference_files(run, files, text=summary)
    question = implementation_question(summary, snapshot, references)
    numbered = numbered_files(files)
    receipt, read = {}, []
    if mode == "one_shot":
        answer = ask(
            user, app.pk, "implementation", IMPLEMENTATION_INSTRUCTIONS, question, numbered,
            receipt,
        )
    else:
        def emit(event, data):
            if event == "tool" and data.get("name") == "read_file" and data.get("detail"):
                read.append(data["detail"])

        answer = agent_answer(
            user, app.pk, snapshot.pk, "implementation", IMPLEMENTATION_INSTRUCTIONS,
            question, numbered, receipt, emit=emit,
        ).answer
    return answer, receipt, read, [reference["path"] for reference in references], held


def run(user, app, modes=MODES):
    """Every ticket every way. Returns the record `save` writes."""
    from .ai import cost
    from .models import AIConfiguration

    pack = load_pack()
    snapshot = snapshot_for(app, pack)
    config = AIConfiguration.objects.filter(
        application=app, purpose="plan_drafting", enabled=True
    ).first()
    details = []
    for key, item in pack["tickets"].items():
        for mode in modes:
            started = time.monotonic()
            try:
                answer, receipt, read, references, held = ask_ticket(
                    user, app, snapshot, item, mode
                )
                failure = ""
            except ValidationError as refused:
                answer, receipt, read, references, held = "", {}, [], [], {}
                failure = " ".join(refused.messages)[:300]
            found = score(item, held, answer) if held else {
                "answered": False, "changed": [], "expect": [], "forbid": [], "fixed": False,
            }
            seen = set(references) | set(read)
            spent = (
                cost(config, receipt["prompt_tokens"], receipt["completion_tokens"])
                if config and receipt
                else 0
            )
            details.append(
                {
                    "ticket": key,
                    "mode": mode,
                    **found,
                    "failure": failure,
                    "needs_seen": sorted(path for path in item["needs"] if path in seen),
                    "needs": item["needs"],
                    "read": read,
                    "prompt_tokens": receipt.get("prompt_tokens", 0),
                    "completion_tokens": receipt.get("completion_tokens", 0),
                    "turns": receipt.get("turns", 1 if receipt else 0),
                    "tool_calls": receipt.get("tool_calls", 0),
                    "cost": str(spent),
                    "seconds": round(time.monotonic() - started, 1),
                }
            )
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "provider": getattr(config, "provider", ""),
        "model": getattr(config, "model", ""),
        "pack": pack_digest(pack),
        "snapshot": {"repository": snapshot.repository.name, "number": snapshot.number},
        "metrics": metrics(details, modes),
        "details": details,
    }


def metrics(details, modes=MODES):
    """Per way: tickets fixed and answered, checks met, dependencies seen, cost."""
    from decimal import Decimal

    found = {}
    for mode in modes:
        rows = [row for row in details if row["mode"] == mode]
        checks = [check for row in rows for check in row["expect"] + row["forbid"]]
        needs = sum(len(row["needs"]) for row in rows)
        found[mode] = {
            "tickets": len(rows),
            "fixed": sum(row["fixed"] for row in rows),
            "answered": sum(row["answered"] for row in rows),
            "failed": sum(bool(row["failure"]) for row in rows),
            "checks_met": round(100 * sum(c["met"] for c in checks) / max(1, len(checks)), 1),
            "needs_seen": round(
                100 * sum(len(row["needs_seen"]) for row in rows) / max(1, needs), 1
            ),
            "cost": str(sum((Decimal(row["cost"]) for row in rows), Decimal("0"))),
            "turns": sum(row["turns"] for row in rows),
            "tool_calls": sum(row["tool_calls"] for row in rows),
            "seconds": round(sum(row["seconds"] for row in rows), 1),
        }
    return found


def verdict(found):
    """Whether tools clear the plan's bar against one-shot, and why not if they do not."""
    from decimal import Decimal

    if not {"one_shot", "tools"} <= set(found):
        return "Run both ways to compare."
    from .agent_measurements import NOTHING_COMPARED, nothing_compared

    if nothing_compared(found):
        return NOTHING_COMPARED
    alone, tools = found["one_shot"], found["tools"]
    reasons = []
    if tools["fixed"] < alone["fixed"]:
        reasons.append(f"fixed {tools['fixed']} tickets against {alone['fixed']}")
    if tools["checks_met"] < alone["checks_met"]:
        reasons.append(f"met {tools['checks_met']}% of checks against {alone['checks_met']}%")
    base = Decimal(alone["cost"])
    if base and Decimal(tools["cost"]) > COST_BAR * base:
        reasons.append(f"cost ${tools['cost']} against ${alone['cost']} (over {COST_BAR}x)")
    if not reasons and tools["fixed"] == alone["fixed"] and (
        tools["checks_met"] == alone["checks_met"]
    ):
        return "Tools are no better than one-shot on this pack: leave them off."
    if reasons:
        return "Tools do not clear the bar: " + "; ".join(reasons) + "."
    return "Tools clear the bar: switch code_factory_agent_tools on."


def save(record):
    RECORDS.mkdir(parents=True, exist_ok=True)
    stamp = record["generated_at"][:16].replace(":", "")
    model = re.sub(r"[^\w.-]+", "-", record["model"] or "model")
    path = RECORDS / f"{stamp}-{model}.json"
    path.write_text(json.dumps(record, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
