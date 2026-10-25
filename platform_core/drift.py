"""LLM drift: is the model still doing the job it did, on the same inputs?

A provider can change a model behind the same name, a team can switch to a
cheaper one, and a prompt can be edited. Each can quietly change how many
relationships survive verification, whether answers cite the right source,
and what a call costs. The scorecard (`scorecard.py`) never calls a model, so
it cannot see any of this. This does, deliberately and on request.

**Same inputs every run.** A frozen evidence pack (`benchmarks/drift-pack.json`)
holds the extraction sources and the answer evidence, built once from this
repository's corpus with no model. Every run sends exactly that, so a change
in the numbers is the model's - not the graph's, the corpus's or the ranking's.

**Judged the way the platform judges.** Extraction is scored by
`graph_ai.judge`, the same function graph generation uses, so "accepted" means
what it means in production, with every refusal's reason counted. An answer
cites the expected source when one of its `[n]` markers points at it; a marker
pointing at evidence that was never supplied is counted as invented.

**It spends, and says so.** Each run is a handful of calls on the
application's own configuration and credential, through `invoke_ai`: the
monthly budget applies and every call is receipted as usual. It is run as a
named person (`manage.py llm_drift <app> --as <user>`), never automatically.
"""

import hashlib
import json
import re
import time
from pathlib import Path
from statistics import mean
from types import SimpleNamespace

from .scorecard import BENCH, KIT

PACK = BENCH / "drift-pack.json"
RECORDS = BENCH / "drift"
#: Sources sent for extraction, and questions answered, per run.
EXTRACTION_TICKETS = ("CARE-401", "CARE-412", "CARE-425", "CARE-431")
ANSWER_QUESTIONS = 6

#: metric: (label, unit, which way is better, how far it may move from the
#: model's first recorded run before it counts as drift).
METRICS = {
    "extraction_valid": ("Extraction replies that parse", "%", "higher", 0),
    "extraction_acceptance": ("Proposed relationships that verify", "%", "higher", 15),
    "extraction_off_ontology": ("Proposals outside the ontology", "%", "lower", 10),
    "relationships_per_source": ("Relationships proposed per source", "", "higher", 4),
    "answer_cites_expected": ("Answers citing the expected source", "%", "higher", 17),
    "answer_with_citation": ("Answers citing any evidence", "%", "higher", 0),
    "answer_invented_citations": ("Citations to evidence never supplied", "", "lower", 0),
    "tokens_per_call": ("Tokens per call", "", "lower", 50),
    "seconds_per_call": ("Seconds per call", "s", "lower", 100),
}
#: Tokens and seconds drift by proportion, not by points.
PROPORTIONAL = {"tokens_per_call", "seconds_per_call"}


def build_pack():
    """The frozen inputs, from the repository's corpus. No model; a throwaway database."""
    from .graph_ai import citations_from
    from .scorecard import publish, throwaway_database, workspace

    questions = json.loads((KIT / "benchmark-questions.json").read_text(encoding="utf-8"))
    pack = {"extraction": [], "answers": []}
    with throwaway_database():
        ws = workspace()
        revision = publish(ws.app)
        from .models import KnowledgeEntry

        sources = dict(
            (str(pk), source)
            for pk, source in KnowledgeEntry.objects.filter(application=ws.app).values_list(
                "id", "source"
            )
        )
        for care in EXTRACTION_TICKETS:
            ticket = ws.tickets[care]
            pack["extraction"].append({"id": care, "title": ticket.title, "text": ticket.content})
        for question in questions[:ANSWER_QUESTIONS]:
            cited = citations_from(
                ws.app.pk,
                revision.data,
                revision.number,
                question["question"],
                viewer=ws.people["owner"],
            )
            pack["answers"].append(
                {
                    "question": question["question"],
                    "expected": question["expected"],
                    "evidence": [
                        {
                            "id": f"e{i}",
                            "title": c["title"],
                            "excerpt": c["excerpt"],
                            # Where it came from: a ticket's key is in its address.
                            "source": sources.get(c["id"], ""),
                        }
                        for i, c in enumerate(cited, 1)
                    ],
                }
            )
    PACK.write_text(json.dumps(pack, indent=1, ensure_ascii=False), encoding="utf-8")
    return pack


def load_pack():
    """The frozen pack. Missing is an error, never a reason to build one.

    It used to build a fresh pack when the file was absent, so a checkout or an
    image without it compared new runs against records of different inputs.
    A new pack starts a new reference; that is `llm_drift --build-pack`, asked
    for by name.
    """
    from django.core.exceptions import ImproperlyConfigured

    if not PACK.is_file():
        raise ImproperlyConfigured(
            f"{PACK.name} is missing. Restore it from the repository; "
            "`manage.py llm_drift --build-pack` makes a new one, which starts a new reference."
        )
    return json.loads(PACK.read_text(encoding="utf-8"))


def proposal(task_id, item, reason):
    """One proposed relationship as the record keeps it, so a change to
    `judge` can be re-scored against what the model actually said, for free."""
    fields = ("subject", "subject_type", "relation", "object", "object_type", "quote")
    kept = {k: item.get(k) for k in fields} if isinstance(item, dict) else {"raw": repr(item)}
    return {"task": task_id, **kept, "refused": reason}


def rescore(record):
    """(acceptance %, reasons) for a record's proposals under today's `judge`."""
    from .graph_ai import judge

    pack = {t["id"]: t for t in load_pack()["extraction"]}
    reasons, accepted, proposals = {}, 0, record["details"].get("proposals", [])
    for p in proposals:
        task = pack[p["task"]]
        item = {k: v for k, v in p.items() if k not in ("task", "refused")}
        item["source_id"] = task["id"]
        source = {"title": task["title"], "excerpt": task["text"]}
        _, reason = judge(item, {task["id"]: source})
        if reason:
            reasons[reason] = reasons.get(reason, 0) + 1
        else:
            accepted += 1
    return (round(100 * accepted / len(proposals), 1) if proposals else 0.0), reasons


def markers(answer):
    """The evidence numbers an answer cites, as [3] or [3][4] or [3, 4]."""
    found = []
    for group in re.findall(r"\[(\d+(?:\s*,\s*\d+)*)\]", answer):
        found.extend(int(n) for n in re.split(r"\s*,\s*", group))
    return found


def run(user, app):
    """One drift run on this application's configured models. Returns the record."""
    from django.core.exceptions import ValidationError
    from django.utils import timezone

    from .ai import invoke_ai
    from .benchmark import matches
    from .graph_ai import extract_relationships, judge
    from .models import AIConfiguration

    pack = load_pack()
    calls = []
    proposed = accepted = off = valid = 0
    reasons, proposals = {}, []
    for task in pack["extraction"]:
        citation = {
            "id": task["id"],
            "title": task["title"],
            "excerpt": task["text"],
            "digest": hashlib.sha256(task["text"].encode()).hexdigest(),
        }
        receipt, start = {}, time.perf_counter()
        try:
            found = extract_relationships(
                app.pk, SimpleNamespace(configured_by=user), citation, receipt=receipt
            )
            valid += 1
        except ValidationError:
            found = []
        calls.append((time.perf_counter() - start, receipt))
        for item in found:
            proposed += 1
            _, reason = judge(item, {task["id"]: citation})
            proposals.append(proposal(task["id"], item, reason))
            if reason:
                reasons[reason] = reasons.get(reason, 0) + 1
                off += reason == "outside the ontology"
            else:
                accepted += 1

    cites_expected = with_citation = invented = 0
    answers = []
    for task in pack["answers"]:
        evidence = task["evidence"]
        receipt, start = {}, time.perf_counter()
        answer = invoke_ai(
            user, app.pk, "graph_retrieval", task["question"], evidence, receipt=receipt
        )
        calls.append((time.perf_counter() - start, receipt))
        numbers = markers(answer)
        good = [n for n in numbers if 1 <= n <= len(evidence)]
        invented += len(numbers) - len(good)
        with_citation += bool(good)
        hit = any(
            matches(
                SimpleNamespace(
                    title=evidence[n - 1]["title"], source=evidence[n - 1].get("source", "")
                ),
                task["expected"],
            )
            for n in set(good)
        )
        cites_expected += hit
        answers.append({"question": task["question"], "cited": sorted(set(good)), "hit": hit})

    tokens = [r.get("prompt_tokens", 0) + r.get("completion_tokens", 0) for _, r in calls if r]
    graph_config = AIConfiguration.objects.filter(
        application=app, purpose="graph_generation"
    ).first()
    answer_config = AIConfiguration.objects.filter(
        application=app, purpose="graph_retrieval"
    ).first()
    n_extract, n_answer = len(pack["extraction"]), len(pack["answers"])
    values = {
        "extraction_valid": round(100 * valid / n_extract, 1),
        "extraction_acceptance": round(100 * accepted / proposed, 1) if proposed else 0.0,
        "extraction_off_ontology": round(100 * off / proposed, 1) if proposed else 0.0,
        "relationships_per_source": round(proposed / n_extract, 1),
        "answer_cites_expected": round(100 * cites_expected / n_answer, 1),
        "answer_with_citation": round(100 * with_citation / n_answer, 1),
        "answer_invented_citations": invented,
        "tokens_per_call": round(mean(tokens)) if tokens else 0,
        "seconds_per_call": round(mean(t for t, _ in calls), 1),
    }
    return {
        "generated_at": timezone.now().isoformat(timespec="seconds"),
        "extraction_model": f"{graph_config.provider}:{graph_config.model}" if graph_config else "",
        "answer_model": f"{answer_config.provider}:{answer_config.model}" if answer_config else "",
        "calls": len(calls),
        "metrics": values,
        "details": {"rejection_reasons": reasons, "proposals": proposals, "answers": answers},
    }


def history(record):
    """Earlier records for the same pair of models, oldest first."""
    if not RECORDS.is_dir():
        return []
    same = []
    for path in sorted(RECORDS.glob("*.json")):
        earlier = json.loads(path.read_text(encoding="utf-8"))
        if (earlier.get("extraction_model"), earlier.get("answer_model")) == (
            record["extraction_model"],
            record["answer_model"],
        ):
            same.append(earlier)
    return same


def drifted(record, reference):
    """[(metric, was, now)] that moved the wrong way past their tolerance."""
    found = []
    for name, (_, _, better, tolerance) in METRICS.items():
        was, now = reference["metrics"].get(name), record["metrics"].get(name)
        if was is None or now is None:
            continue
        if name in PROPORTIONAL:
            limit = was * (1 + tolerance / 100)
            worse = now > limit if better == "lower" else now < was * (1 - tolerance / 100)
        else:
            worse = now < was - tolerance if better == "higher" else now > was + tolerance
        if worse:
            found.append((name, was, now))
    return found


def save(record):
    RECORDS.mkdir(parents=True, exist_ok=True)
    stamp = record["generated_at"][:16].replace(":", "")
    model = re.sub(r"[^\w.-]+", "-", record["answer_model"] or "model")
    path = Path(RECORDS) / f"{stamp}-{model}.json"
    path.write_text(json.dumps(record, indent=1, ensure_ascii=False), encoding="utf-8")
    return path
