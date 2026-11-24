"""How each agent measured against one call, and how it is used here (its agent page).

Two readings, neither calling a model:

- **Measurements.** What a pack command recorded with `--record` - `factory_pack`,
  `review_pack`, `triage_pack`, `chat_pack`, `docs_pack`, `document_review_pack` (paid)
  and `learning_upkeep`
  (free) - kept as one `AgentMeasurement` row each beside the JSON file the command
  already writes under `benchmarks/`. Numbers only: each way's totals as the pack's
  own `metrics` worked them out, the verdict its own `verdict` gave, the model
  measured. `comparison` is the latest one drawn as paired bars, `trend` every
  recorded run of the agent's main figure, one line each way, a new model or a new
  pack marked as a new reference. `load` reads a JSON record made elsewhere (a
  development machine) into this database, once.
- **Use.** `use`: the last `ACTIVITY_DAYS` of the agent's `AgentStep` rows - runs,
  tool calls a run, the share of calls that met a limit or a refusal - and, where
  usage reports are on, the receipts of its AI task.

Drawn on the server as SVG coordinates and classes, never a style attribute, so the
page needs no script and the CSP holds - the way `graph_trend.chart` draws.
A measurement says which way is better on a frozen pack; switching tools on stays an
operator's decision in the configuration.
"""

import json
import statistics
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.core.exceptions import ValidationError
from django.db.models import Count, Sum
from django.utils import timezone

from .models.ai import MEASUREMENT_RUNTIMES, MEASUREMENT_VERDICTS

ROOT = Path(__file__).resolve().parent.parent

#: Fewer drafted questions decided than this are too few to judge Knowledge upkeep.
MIN_DECIDED = 10

#: Tool-call outcomes that mean a run met a limit or lost access.
PROBLEMS = ("budget", "refused", "failed")


@dataclass(frozen=True)
class Figure:
    key: str
    label: str
    #: "higher" or "lower" is better; "" is said and never judged.
    better: str
    #: "%", "$", "s", or "" for a count; `of` names the count it is out of.
    unit: str = ""
    of: str = ""
    cost: bool = False


@dataclass(frozen=True)
class Pack:
    key: str
    #: The `agent_catalog` key it measures.
    agent: str
    command: str
    #: (mode, label) pairs: the way it is compared against first.
    modes: tuple
    figures: tuple
    #: The figure the trend draws, as a share from 0 to 1, and what it is called.
    main_label: str
    #: The module whose `verdict` judges its metrics; "" when the record carries one.
    judge: str = ""
    #: The folder under `benchmarks/` its command records into.
    folder: str = ""
    paid: bool = True


AGAINST = (("one_shot", "One call"), ("tools", "With tools"))
TIMING = (
    Figure("turns", "Turns", ""),
    Figure("tool_calls", "Tool calls", ""),
    Figure("seconds", "Seconds", "lower", "s"),
    Figure("cost", "Cost", "lower", "$", cost=True),
)

PACKS = {
    pack.key: pack
    for pack in (
        Pack(
            "factory_pack",
            "code-factory",
            "manage.py factory_pack <application id> --as <user> --record",
            AGAINST,
            (
                Figure("fixed", "Tickets fixed", "higher", of="tickets"),
                Figure("checks_met", "Checks met", "higher", "%"),
                Figure("needs_seen", "Dependencies in front of it", "higher", "%"),
                *TIMING,
            ),
            "Checks met",
            judge="factory_pack",
            folder="factory",
        ),
        Pack(
            "review_pack",
            "code-review",
            "manage.py review_pack <application id> --as <user> --record",
            AGAINST,
            (
                Figure("found", "Changes found", "higher", of="changes"),
                Figure("noise", "Findings that are noise", "lower"),
                *TIMING,
            ),
            "Changes found",
            judge="review_pack",
            folder="review",
        ),
        Pack(
            "triage_pack",
            "triage",
            "manage.py triage_pack <application id> --as <user> --record",
            AGAINST,
            (
                Figure("cause_cited", "Confirmed cause cited", "higher", of="cases"),
                Figure("cause_first", "Cited first", "higher", of="cases"),
                Figure("mrr", "Ranking (MRR)", "higher"),
                Figure("cause_in_evidence", "Cause in its evidence", "higher", of="cases"),
                *TIMING,
            ),
            "Ranking (MRR)",
            judge="triage_pack",
            folder="triage",
        ),
        Pack(
            "chat_pack",
            "chat",
            "manage.py chat_pack <application id> --as <user> --record",
            AGAINST,
            (
                Figure("cited_expected", "Expected source cited", "higher", of="questions"),
                Figure("cited_first", "Cited first", "higher", of="questions"),
                Figure("shown_expected", "Expected source in front of it", "higher",
                       of="questions"),
                Figure("invented", "Citations to nothing", "lower"),
                Figure("failed", "Questions unanswered", "lower"),
                *TIMING,
            ),
            "Expected source cited",
            judge="chat_pack",
            folder="chat",
        ),
        Pack(
            "docs_pack",
            "code-docs",
            "manage.py docs_pack <application id> --as <user> --record",
            AGAINST,
            (
                Figure("components", "Components described", "higher", of="asked"),
                Figure("paragraphs", "Paragraphs kept", "higher"),
                Figure("files_quoted", "Files quoted", "higher"),
                Figure("kept_share", "Paragraphs that kept their quotes", "higher", "%"),
                *TIMING,
            ),
            "Components described",
            judge="docs_pack",
            folder="docs",
        ),
        Pack(
            "document_review_pack",
            "document-review",
            "manage.py document_review_pack <application id> --as <user> --record",
            AGAINST,
            (
                Figure("found", "Planted contradictions found", "higher", of="items"),
                Figure("linked", "Linked to a source that contradicts them", "higher",
                       of="items"),
                Figure("findings", "Findings kept", ""),
                Figure("failed", "Items unanswered", "lower"),
                *TIMING,
            ),
            "Planted contradictions found",
            judge="document_review_pack",
            folder="document-review",
        ),
        Pack(
            "analysis_pack",
            "gap-analysis",
            "manage.py analysis_pack <application id> --as <user> --record",
            AGAINST,
            (
                Figure("cited", "Planted identifiers its items cite", "higher", of="expected"),
                Figure("offered", "In what it was handed", "", of="expected"),
                Figure("found_more", "More it found itself", "higher"),
                Figure("evidenced", "Items with evidence", "higher", of="items"),
                Figure("failed", "Tickets unanswered", "lower"),
                *TIMING,
            ),
            "Planted identifiers its items cite",
            judge="analysis_pack",
            folder="analysis",
        ),
        Pack(
            "learning_upkeep",
            "draft-a-fix",
            "manage.py learning_upkeep <application id> --record",
            (("rules", "No draft"), ("drafted", "Drafted")),
            (
                Figure("decided", "Questions decided", ""),
                Figure("accept_rate", "Accepted", "higher", "%"),
                Figure("as_drafted", "Accepted with the drafted source", "", of="accepted"),
            ),
            "Accepted",
            paid=False,
        ),
    )
}


def pack_for(agent_key):
    for pack in PACKS.values():
        if pack.agent == agent_key:
            return pack
    return None


def main_share(pack, totals):
    """The pack's main figure for one way, as a share from 0 to 1, or None."""
    if not totals:
        return None
    if pack.key == "factory_pack":
        return _number(totals.get("checks_met")) / 100
    if pack.key == "review_pack":
        return _share(totals.get("found"), totals.get("changes"))
    if pack.key == "triage_pack":
        return _number(totals.get("mrr"))
    if pack.key == "chat_pack":
        return _share(totals.get("cited_expected"), totals.get("questions"))
    if pack.key == "docs_pack":
        return _share(totals.get("components"), totals.get("asked"))
    if pack.key == "document_review_pack":
        return _share(totals.get("found"), totals.get("items"))
    if pack.key == "analysis_pack":
        return _share(totals.get("cited"), totals.get("expected"))
    return _number(totals.get("accept_rate")) / 100


def _share(part, whole):
    whole = _number(whole)
    return _number(part) / whole if whole else None


def _number(value):
    """A recorded figure as a float; a cost is stored as a decimal string."""
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return 0.0


def numbers_only(metrics, modes):
    """Each way's totals with every value a number (a cost as its decimal string):
    whatever else a record carries stays in its JSON file."""
    kept = {}
    for mode, _ in modes:
        totals = metrics.get(mode)
        if not isinstance(totals, dict):
            continue
        row = {}
        for key, value in totals.items():
            if isinstance(value, bool) or not isinstance(value, int | float | str):
                continue
            if isinstance(value, str):
                try:
                    Decimal(value)
                except InvalidOperation:
                    continue
            row[str(key)[:40]] = value
        kept[mode] = row
    return kept


#: What a pack's verdict says when one way could ask nothing: never "no better",
#: which would put a judgement on an agent's page that no comparison made.
NOTHING_COMPARED = (
    "Nothing was compared: one way could ask none of the pack's cases here - see each "
    "case's reason above, set up what it names and run it again."
)
#: What it says when the tools way never called a tool: it was one call twice, so
#: whatever it scored says nothing about tools - `docs_pack`'s first real run
#: "failed the bar" on 12 paragraphs against 13 that way.
TOOLS_UNUSED = (
    "Nothing was compared: the tools way never called a tool, so it was one call "
    "asked twice. Look at what the agent is told about its tools, and run it again."
)
#: The count of cases each pack's totals carry, by whatever it calls them.
CASES = ("items", "tickets", "questions", "cases", "changes")


def tools_unused(found):
    """Whether the tools way asked its cases and never called a tool.

    Only a pack whose totals count tool calls can say; one that does not is never
    read as unused.
    """
    tools = (found or {}).get("tools")
    if not isinstance(tools, dict) or "tool_calls" not in tools:
        return False
    cases = next((tools[key] for key in CASES if key in tools), None)
    asked = cases is None or (cases or 0) > (tools.get("failed") or 0)
    return asked and not tools["tool_calls"]


def why_nothing_compared(found):
    """The sentence a verdict gives for a pack run `nothing_compared` refuses."""
    return TOOLS_UNUSED if tools_unused(found) and not _uncompared(found) else NOTHING_COMPARED


def nothing_compared(found):
    """Whether a pack's totals compare nothing: either way asked none of its cases
    (every one failed, or there were none); both ways failed most of them - a
    provider refusing calls, a rate limit, not an agent being worse; both ways
    kept nothing to score (code documentation describing no component either way);
    or the tools way never called a tool (`tools_unused`)."""
    return _uncompared(found) or tools_unused(found)


def _uncompared(found):
    ways = [totals for totals in (found or {}).values() if isinstance(totals, dict)]
    mostly_failed = []
    for totals in ways:
        cases = next((totals[key] for key in CASES if key in totals), None)
        if cases == 0:
            return True
        failed = totals.get("failed") or 0
        # A pack of one case (code documentation) says only whether it failed.
        if failed and (cases is None or failed >= cases):
            return True
        mostly_failed.append(bool(cases) and failed * 2 > cases)
    if len(ways) > 1 and all(mostly_failed):
        return True
    described = [totals["components"] for totals in ways if "components" in totals]
    return bool(described) and len(described) == len(ways) and not any(described)


def state_of(text):
    """The verdict a pack's `verdict` wrote, as one of `MEASUREMENT_VERDICTS`."""
    for start, state in (
        ("Tools clear the bar", "clears"),
        ("Tools are no better", "no_better"),
        ("Tools do not clear", "fails"),
    ):
        if text.startswith(start):
            return state
    return "incomplete"


def judged(pack, metrics):
    """(state, sentence) for a recorded pack's metrics, by the pack's own `verdict`."""
    import importlib

    module = importlib.import_module(f"platform_core.{pack.judge}")
    text = module.verdict(metrics)
    return state_of(text), text


def upkeep(app, days=30):
    """Knowledge upkeep measured by use: `learning_draft.report` as a record, and
    its verdict - drafted questions accepted more often than ones with no draft."""
    from .learning_draft import report
    from .models import AIConfiguration

    found = report(app, days)
    metrics = {}
    for mode in ("rules", "drafted"):
        row = found[mode]
        metrics[mode] = {
            **row,
            "accept_rate": round(100 * row["accepted"] / row["decided"], 1)
            if row["decided"]
            else 0.0,
            "days": days,
        }
    drafted, rules = metrics["drafted"], metrics["rules"]
    if drafted["decided"] < MIN_DECIDED:
        state = "too_few"
        text = (
            f"{drafted['decided']} drafted question{'s' if drafted['decided'] != 1 else ''} "
            f"decided in {days} days: too few to judge until {MIN_DECIDED}."
        )
    elif not rules["decided"]:
        state = "incomplete"
        text = "Nothing was decided without a draft, so there is nothing to compare with."
    else:
        now, then = drafted["accept_rate"], rules["accept_rate"]
        state = "clears" if now > then else ("no_better" if now == then else "fails")
        text = (
            f"Drafted questions were accepted {now}% of the time, against {then}% with no "
            "draft."
        )
    config = AIConfiguration.objects.filter(application=app, purpose="knowledge_upkeep").first()
    return {
        "generated_at": timezone.now().isoformat(timespec="seconds"),
        "provider": getattr(config, "provider", ""),
        "model": getattr(config, "model", ""),
        "pack": "",
        "metrics": metrics,
        "verdict": text,
        "state": state,
    }


#: How a measured run's calls were carried, as `carried_by` names them.
RUNTIMES = dict(MEASUREMENT_RUNTIMES)


def carried_by(app, pack_key):
    """How the agent a pack measures would run here now: a `RUNTIMES` key, or ""
    when it cannot be told (no configuration, no credential, no catalog entry).

    Read as the harness reads it (`harness.runtime_for`), so a pack record says
    whether its turns went through the CLI - one summed receipt a run, no cache -
    or the Messages API, which caches only with `claude_prompt_caching` on. The
    credential is looked at for its kind and never kept.
    """
    from django.conf import settings
    from django.core.exceptions import ImproperlyConfigured

    from . import agent_catalog
    from .agent_runtime import claude_runtime, messages_runtime, openai_runtime
    from .agent_runtime.harness import runtime_for
    from .ai import provider_credential
    from .models import AIConfiguration

    pack = PACKS.get(pack_key)
    agent = agent_catalog.get(pack.agent) if pack else None
    if agent is None:
        return ""
    config = AIConfiguration.objects.filter(
        application=app, purpose=agent.purpose, enabled=True
    ).first()
    if config is None:
        return ""
    try:
        runtime = runtime_for(config.provider, provider_credential(config, app))
    except ImproperlyConfigured:
        return ""
    if runtime is messages_runtime:
        return "messages-cached" if settings.CLAUDE_PROMPT_CACHING else "messages"
    return {claude_runtime: "cli", openai_runtime: "openai"}.get(runtime, "")


@dataclass(frozen=True)
class Mark:
    """Where a case of a pack run started: the receipts written before it in this
    run (`index`, None when nothing collects them) and when."""

    index: int | None
    since: datetime


def measuring(run):
    """Collect the receipts a pack's `run` writes, in its own context, so each case
    is billed by id (`mark`, `case_receipts`).

    By time alone a case also counted any receipt written meanwhile under the same
    person and purpose: a measured factory run on a development machine whose
    server was in use took in three of that person's own Code Factory calls.
    Another process writes into another context, so its receipts are never here.
    """
    import functools

    @functools.wraps(run)
    def collecting(*args, **kwargs):
        from .services import RECEIPTS_WRITTEN

        token = RECEIPTS_WRITTEN.set([])
        try:
            return run(*args, **kwargs)
        finally:
            RECEIPTS_WRITTEN.reset(token)

    return collecting


def mark():
    """Where the next case starts, for `case_receipts`."""
    from .services import RECEIPTS_WRITTEN

    written = RECEIPTS_WRITTEN.get()
    return Mark(None if written is None else len(written), timezone.now())


def case_receipts(receipts, since, counted):
    """(turns, prompt tokens, completion tokens, amount) of the receipts one case of
    a pack run wrote, less any an earlier case already counted (`counted`, updated
    here). `since` is the case's `mark`: inside `measuring`, the receipts its own
    calls wrote, by id; otherwise - or given a time - those of `receipts` written
    since it started.

    A receipt written in the clock tick a case starts in reads as that case's too
    by time alone; counting the one before it again flipped a test's verdict one
    run in three, and would add a case's cost to the next on a fast provider.
    """
    from .services import RECEIPTS_WRITTEN

    written = RECEIPTS_WRITTEN.get()
    if isinstance(since, Mark) and since.index is not None and written is not None:
        receipts = receipts.filter(pk__in=written[since.index :])
    else:
        receipts = receipts.filter(created_at__gte=getattr(since, "since", since))
    rows = list(
        receipts
        .exclude(pk__in=counted)
        .values_list("pk", "turns", "input_tokens", "output_tokens", "amount")
    )
    counted.update(row[0] for row in rows)
    return (
        sum(row[1] for row in rows),
        sum(row[2] for row in rows),
        sum(row[3] for row in rows),
        sum((row[4] for row in rows), Decimal("0")),
    )


def unused(path):
    """`path`, or the same name ending -2, -3... when it is taken.

    Pack records are named by the minute; two runs of a quick pack in one minute
    (`measure_agents --repeat`) wrote the second over the first, and both rows then
    named one file.
    """
    path = Path(path)
    number = 2
    found = path
    while found.exists():
        found = path.with_name(f"{path.stem}-{number}{path.suffix}")
        number += 1
    return found


def combine(states, repeat):
    """(state, sentence) for `repeat` runs of one pack, from the states those that
    compared something were recorded with.

    Packs are small, so one run is not a verdict: tools clear only when most of the
    runs asked for cleared - two of three - and a run that compared nothing counts
    as one that did not clear, never as one left out.
    """
    cleared = sum(state == "clears" for state in states)
    counts = ", ".join(
        f"{sum(state == key for state in states)} {label.lower()}"
        for key, label in MEASUREMENT_VERDICTS
        if any(state == key for state in states)
    )
    missing = repeat - len(states)
    said = f"{counts}{', ' if counts and missing else ''}"
    said += f"{missing} compared nothing" if missing else ""
    if cleared * 2 > repeat:
        return "clears", f"Clears the bar in {cleared} of {repeat} runs ({said})."
    if not states:
        return "incomplete", f"No run of {repeat} compared anything."
    return (
        "fails" if cleared else max(set(states), key=states.count),
        f"Does not clear the bar: cleared in {cleared} of {repeat} runs ({said}).",
    )


def stamp(app, pack_key, found):
    """Write how the run was carried into a pack record before it is saved."""
    found.setdefault("runtime", carried_by(app, pack_key))
    return found


def record(app, pack_key, found, user=None, path=None):
    """Keep one pack record, written to `path`, as an `AgentMeasurement`. Returns
    (row, created): the same record kept twice is the one row, updated."""
    from .models import AgentMeasurement

    pack = PACKS[pack_key]
    metrics = numbers_only(found.get("metrics") or {}, pack.modes)
    if pack.judge:
        state, text = judged(pack, metrics)
    else:
        state, text = found.get("state", "incomplete"), found.get("verdict", "")
    try:
        generated = datetime.fromisoformat(str(found.get("generated_at", "")))
    except ValueError:
        raise ValidationError("The record has no readable generated_at.") from None
    if timezone.is_naive(generated):
        generated = generated.replace(tzinfo=UTC)
    return AgentMeasurement.objects.update_or_create(
        application=app,
        pack=pack.key,
        generated_at=generated,
        model=str(found.get("model") or "")[:160],
        defaults={
            "agent": pack.agent,
            "provider": str(found.get("provider") or "")[:80],
            "pack_digest": str(found.get("pack") or "")[:64],
            "runtime": runtime if (runtime := str(found.get("runtime") or "")) in RUNTIMES
            else "",
            "metrics": metrics,
            "verdict": state,
            "verdict_text": text[:300],
            "recorded_by": user,
            "file": relative(path)[:200] if path else "",
        },
    )


def relative(path):
    path = Path(path).resolve()
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.name


def load(app, path, user=None):
    """Read a pack's recorded JSON file into this database. The pack is the folder
    under `benchmarks/` it was recorded into."""
    path = Path(path)
    folders = {pack.folder: pack for pack in PACKS.values() if pack.folder}
    pack = folders.get(path.resolve().parent.name)
    if pack is None:
        named = ", ".join(f"benchmarks/{folder}" for folder in sorted(folders))
        raise ValidationError(
            f"{path.name} is not under one of {named}, so which pack made it is unknown."
        )
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as failure:
        raise ValidationError(f"{path.name} could not be read: {failure}") from None
    if not isinstance(found, dict) or not isinstance(found.get("metrics"), dict):
        raise ValidationError(f"{path.name} is not a pack record.")
    return record(app, pack.key, found, user, path)


def forget(app, path):
    """Remove a record kept by mistake: its rows here and its file. The file must be
    in a pack's folder under `benchmarks/`, as `load` requires. Returns
    (rows removed, whether the file was removed)."""
    from .models import AgentMeasurement

    path = Path(path)
    folders = {pack.folder for pack in PACKS.values() if pack.folder}
    if path.resolve().parent.name not in folders:
        named = ", ".join(f"benchmarks/{folder}" for folder in sorted(folders))
        raise ValidationError(f"{path.name} is not under one of {named}.")
    removed, _ = AgentMeasurement.objects.filter(
        application=app, file__in={relative(path), path.name}
    ).delete()
    existed = path.is_file()
    if existed:
        path.unlink()
    return removed, existed


def measurements(app, agent_key):
    from .models import AgentMeasurement

    return list(AgentMeasurement.objects.filter(application=app, agent=agent_key))


def latest_by_agent(app):
    """{agent key: its newest measurement here}, in one query."""
    from .models import AgentMeasurement

    found = {}
    for row in AgentMeasurement.objects.filter(application=app):
        found[row.agent] = row
    return found


# Paired bars: a label column, then each way's bar and its value.
BAR_WIDTH, BAR_LABEL, BAR_SPAN, BAR_ROW, BAR_HEIGHT = 640, 210, 330, 46, 13


def _shown(figure, value, total):
    if figure.unit == "$":
        return f"${_number(value):.4f}"
    if figure.unit == "%":
        return f"{_number(value):g}%"
    if figure.unit == "s":
        return f"{_number(value):g} s"
    if figure.of:
        return f"{_number(value):g} of {_number(total):g}"
    return f"{_number(value):g}"


def _direction(figure, before, after):
    if not figure.better or before == after:
        return "same" if figure.better else ""
    up = after > before
    return "better" if up == (figure.better == "higher") else "worse"


def comparison(row, show_cost=True):
    """The measurement as paired bars, each figure one row: the way it is compared
    against, then the other, each bar scaled to the larger of the two (to 100 for a
    percentage, to its count for one out of a count)."""
    pack = PACKS.get(row.pack)
    if pack is None:
        return None
    (first, first_label), (second, second_label) = pack.modes
    totals = row.metrics or {}
    if first not in totals and second not in totals:
        return None
    rows = []
    for figure in pack.figures:
        if figure.cost and not show_cost:
            continue
        values = []
        for mode in (first, second):
            mode_totals = totals.get(mode)
            if not mode_totals or figure.key not in mode_totals:
                values.append(None)
                continue
            values.append(_number(mode_totals[figure.key]))
        if values == [None, None]:
            continue
        scale = max(value or 0 for value in values)
        if figure.unit == "%":
            scale = 100.0
        elif figure.of:
            counts = [_number((totals.get(mode) or {}).get(figure.of)) for mode in (first, second)]
            scale = max([scale, *counts])
        top = len(rows) * BAR_ROW
        bars = []
        for index, (mode, value) in enumerate(zip((first, second), values, strict=True)):
            if value is None:
                bars.append(None)
                continue
            total = (totals.get(mode) or {}).get(figure.of)
            bars.append(
                {
                    "mode": "base" if index == 0 else "agent",
                    "y": top + 6 + index * (BAR_HEIGHT + 4),
                    "text_y": top + 6 + index * (BAR_HEIGHT + 4) + BAR_HEIGHT - 2,
                    "length": round(BAR_SPAN * value / scale, 1) if scale else 0,
                    "text_x": BAR_LABEL
                    + (round(BAR_SPAN * value / scale, 1) if scale else 0)
                    + 6,
                    "shown": _shown(figure, value, total),
                }
            )
        direction = (
            _direction(figure, values[0], values[1]) if None not in values else ""
        )
        rows.append(
            {
                "label": figure.label,
                "label_y": top + 6 + BAR_HEIGHT + 2,
                "bars": [bar for bar in bars if bar],
                "direction": direction,
                "line_y": top + BAR_ROW - 4,
            }
        )
    return {
        "width": BAR_WIDTH,
        "height": max(1, len(rows)) * BAR_ROW,
        "label_x": BAR_LABEL - 12,
        "bar_x": BAR_LABEL,
        "bar_height": BAR_HEIGHT,
        "rows": rows,
        "first": first_label,
        "second": second_label,
        "description": "; ".join(
            f"{item['label']}: "
            + ", ".join(
                f"{label} {bar['shown']}"
                for label, bar in zip((first_label, second_label), item["bars"], strict=False)
            )
            for item in rows
        ),
    }


TREND_WIDTH, TREND_HEIGHT, TREND_LEFT, TREND_RIGHT, TREND_TOP, TREND_BOTTOM = (
    640, 200, 44, 16, 16, 34,
)


def trend(rows):
    """Every recorded measurement's main figure, one line each way, as SVG
    coordinates. A model, a pack or a runtime different from the run before starts
    a new reference, marked by a dashed line: runs either side of it are not
    comparable. A record from before runtimes were kept says nothing, and is not
    read as a change.
    None with fewer than two measurements."""
    if len(rows) < 2:
        return None
    pack = PACKS.get(rows[-1].pack)
    if pack is None:
        return None
    span = len(rows) - 1
    inner_w = TREND_WIDTH - TREND_LEFT - TREND_RIGHT
    inner_h = TREND_HEIGHT - TREND_TOP - TREND_BOTTOM

    def x(index):
        return round(TREND_LEFT + inner_w * index / span, 1)

    def y(value):
        return round(TREND_TOP + inner_h * (1 - min(max(value, 0.0), 1.0)), 1)

    lines = {}
    for name, (mode, label) in zip(("base", "agent"), pack.modes, strict=True):
        points = []
        for index, row in enumerate(rows):
            share = main_share(pack, (row.metrics or {}).get(mode))
            if share is None:
                continue
            points.append(
                {
                    "x": x(index),
                    "y": y(share),
                    "value": round(share, 3),
                    "when": row.generated_at,
                    "model": row.model,
                }
            )
        lines[name] = {
            "label": label,
            "points": points,
            "path": " ".join(f"{p['x']},{p['y']}" for p in points),
        }
    references = [
        {"x": round((x(index - 1) + x(index)) / 2, 1)}
        for index in range(1, len(rows))
        if (rows[index].model, rows[index].pack_digest)
        != (rows[index - 1].model, rows[index - 1].pack_digest)
        or (rows[index].runtime and rows[index - 1].runtime
            and rows[index].runtime != rows[index - 1].runtime)
    ]
    return {
        "width": TREND_WIDTH,
        "height": TREND_HEIGHT,
        "left": TREND_LEFT,
        "right": TREND_WIDTH - TREND_RIGHT,
        "top": TREND_TOP,
        "bottom": TREND_HEIGHT - TREND_BOTTOM,
        "label_y": TREND_HEIGHT - TREND_BOTTOM + 18,
        "grid": [{"y": y(value), "label": f"{value:.2f}"} for value in (0, 0.25, 0.5, 0.75, 1)],
        # The last label ends at its point, so it is not cut off at the edge.
        "ticks": [
            {
                "x": x(index),
                "label": timezone.localtime(row.generated_at).strftime("%b %d"),
                "anchor": "end" if index == span else "middle",
            }
            for index, row in enumerate(rows)
        ],
        "base": lines["base"],
        "agent": lines["agent"],
        "references": references,
        "main_label": pack.main_label,
    }


def use(agent, app, days, show_cost=False):
    """What the agent did here lately, from its steps (and its task's receipts when
    `show_cost`): counts, shares and durations - never what was asked or read."""
    from .models import AgentStep, AIUsage

    since = timezone.now() - timedelta(days=days)
    steps = AgentStep.objects.filter(
        application=app, purpose=agent.purpose, created_at__gte=since
    ).exclude(**{f"{agent.steps_field}__isnull": True})
    if agent.phases:
        steps = steps.filter(phase__in=agent.phases)
    steps = steps.values_list(f"{agent.steps_field}_id", "outcome", "duration_ms")
    rows = list(steps)
    runs = len({row[0] for row in rows})
    problems = sum(row[1] in PROBLEMS for row in rows)
    found = {
        "runs": runs,
        "calls_per_run": round(len(rows) / runs, 1) if runs else 0,
        "problem_share": round(100 * problems / len(rows), 1) if rows else 0,
        "median_ms": int(statistics.median(row[2] for row in rows)) if rows else 0,
        "spend": None,
    }
    if show_cost:
        receipts = AIUsage.objects.filter(
            application=app, purpose=agent.purpose, created_at__gte=since
        )
        found["spend"] = list(
            receipts.values("currency")
            .annotate(amount=Sum("amount"), calls=Count("id"))
            .order_by("currency")
        )
    return found


def page(agent, app, show_cost, days):
    """Everything the agent page draws under "How it measured" and "Here lately"."""
    pack = pack_for(agent.key)
    rows = measurements(app, agent.key) if pack else []
    latest = rows[-1] if rows else None
    return {
        "pack": pack,
        "latest": latest,
        "comparison": comparison(latest, show_cost) if latest else None,
        "trend": trend(rows),
        "count": len(rows),
        "use": use(agent, app, days, show_cost),
        "show_cost": show_cost,
    }
