"""The platform scorecard: one reproducible measurement across every aspect.

Retrieval quality is one number among several things this platform promises:
that every citation is real, that personal data does not reach a model when an
owner says so, that a person sees only what their grant and source system
allow, that AI spend is capped and not repeated, and that answers are quick.
This measures all of them the same way every time, so a change is judged by
what it did rather than by what it was meant to do.

**Reproducible by construction.** It builds its own workspace from files in
this repository - the CarePath documents (converted by the same
`scripts/extract_text.py` production uses), the Jira export (imported through
the real Jira connector code) and the benchmark questions - inside a
throwaway test database. Nothing reads or writes the local database, no model
is called, nothing is charged, and two runs on one commit give the same
quality figures. Timings are the only machine-dependent numbers, and their
tolerance says so.

`manage.py scorecard` prints it; `--record` saves it to `benchmarks/history/`;
`--check` compares it with `benchmarks/baseline.json` and exits non-zero on
any regression - the fifth verification gate.
"""

import hashlib
import json
import re
import statistics
import subprocess
import sys
import time
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test.utils import override_settings

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "demo-artifacts" / "docs"
#: Documents with one planted problem each, for the Source check; some need OCR.
SAMPLES = DOCS / "08-quality-samples"
KIT = ROOT / "demo-artifacts" / "demo-kit"
BENCH = ROOT / "benchmarks"
CACHE = ROOT / ".runtime" / "scorecard-cache"
JIRA_SITE = "https://ajitk15.atlassian.net"

#: Each metric: aspect, label, unit, which way is better, and how far it may
#: move before `--check` calls it a regression. "exact" means it must equal
#: the baseline - a leak count is 0 or it is a failure.
METRICS = {
    # Retrieval quality - the 16 CarePath questions.
    "graph_found": ("Retrieval", "Graph answers: right source found", "of 16", "higher", 0),
    "graph_top3": ("Retrieval", "Graph answers: right source in top 3", "of 16", "higher", 0),
    "graph_mrr": ("Retrieval", "Graph answers: ranking quality (MRR)", "", "higher", 0.005),
    "keyword_found": ("Retrieval", "Keyword answers: right source found", "of 16", "higher", 0),
    "keyword_top3": ("Retrieval", "Keyword answers: right source in top 3", "of 16", "higher", 0),
    "keyword_mrr": ("Retrieval", "Keyword answers: ranking quality (MRR)", "", "higher", 0.005),
    # The same keyword answers with candidates chosen from stored passages
    # (`chunks.py`) - measured beside the path that answers today, so the
    # switch is a comparison rather than a hope.
    "chunk_found": ("Retrieval", "Passage answers: right source found", "of 16", "higher", 0),
    "chunk_top3": ("Retrieval", "Passage answers: right source in top 3", "of 16", "higher", 0),
    "chunk_mrr": ("Retrieval", "Passage answers: ranking quality (MRR)", "", "higher", 0.005),
    # Citation integrity.
    "citations_verified": (
        "Citation integrity",
        "Citations that re-verify against their source",
        "%",
        "exact",
        0,
    ),
    "stale_citations": (
        "Citation integrity",
        "Citations still given after their source changed",
        "",
        "exact",
        0,
    ),
    # Agentic evidence - what Code Factory is handed for each seeded ticket.
    "seeded_evidence_recall": (
        "Agentic evidence",
        "Planted-bug identifiers in the evidence",
        "%",
        "higher",
        0.5,
    ),
    "seeded_tickets_self_cited": (
        "Agentic evidence",
        "Tickets whose evidence cites the ticket itself",
        "of 6",
        "higher",
        0,
    ),
    # The ontology - typing and name merging, against labelled cases.
    "ontology_typing_accuracy": (
        "Ontology",
        "Relationship phrasings typed or refused correctly",
        "%",
        "higher",
        0,
    ),
    "ontology_prompt_coverage": (
        "Ontology",
        "Types and relations the extraction prompt states",
        "%",
        "exact",
        0,
    ),
    "merge_precision": ("Ontology", "Name merges that are right", "%", "higher", 0),
    "merge_recall": ("Ontology", "Same-thing names that merge", "%", "higher", 0),
    # Document types - what each document is, against labels a person gave.
    "document_type_accuracy": (
        "Document types",
        "Documents typed as a person would",
        "%",
        "higher",
        0,
    ),
    "document_type_untitled_accuracy": (
        "Document types",
        "Typed right with no title or file name to go on",
        "%",
        "higher",
        0,
    ),
    # Personal and health data.
    "phi_recall": (
        "Personal data (PHI/PII)",
        "Personal data found by the scan",
        "%",
        "higher",
        0.5,
    ),
    "phi_precision": ("Personal data (PHI/PII)", "Scan findings that are real", "%", "higher", 0.5),
    "restricted_leaks": (
        "Personal data (PHI/PII)",
        "Restricted sources reaching any AI path",
        "",
        "exact",
        0,
    ),
    "permission_leaks": (
        "Personal data (PHI/PII)",
        "Records shown to someone their system denies",
        "",
        "exact",
        0,
    ),
    # HIPAA technical safeguards, 45 CFR 164.312.
    "hipaa_controls_passed": ("HIPAA safeguards", "Safeguard probes passing", "%", "exact", 0),
    # AI cost control.
    "ai_calls_unchanged_rerun": (
        "AI cost control",
        "Model calls re-running unchanged sources",
        "",
        "exact",
        0,
    ),
    "ai_calls_one_change": (
        "AI cost control",
        "Model calls after one source changes",
        "",
        "lower",
        0,
    ),
    "paid_calls_over_budget": (
        "AI cost control",
        "Model calls started past the monthly budget",
        "",
        "exact",
        0,
    ),
    # The gateway (`gateway.py`): a call in flight holds its cost against the
    # cap, and a full set of slots holds the next call back.
    "paid_calls_past_gateway": (
        "AI cost control",
        "Model calls started past a reservation or a full gateway",
        "",
        "exact",
        0,
    ),
    # Graph storage (`graph_store`): versions as rows held across versions.
    "graph_rows_exact": (
        "Graph storage",
        "Graph versions read back exactly from rows",
        "%",
        "exact",
        0,
    ),
    "graph_rows_one_change": (
        "Graph storage",
        "Rows a version writes after one source changes",
        "%",
        "lower",
        2.0,
    ),
    "graph_rows_same": (
        "Graph storage",
        "Graph answers from rows identical to memory",
        "%",
        "exact",
        0,
    ),
    # The release check (`release.py`): what may replace the published version.
    "release_probes_passed": (
        "Release check",
        "Release check probes passing",
        "%",
        "exact",
        0,
    ),
    # Application rules (`rules.py`), against `benchmarks/rules-pack.json`.
    "rules_shown_recall": (
        "Rules",
        "Broken rule among those shown for its ticket",
        "%",
        "higher",
        0,
    ),
    "rules_first_recall": ("Rules", "Broken rule shown first", "%", "higher", 0),
    "rules_quotes_verified": ("Rules", "Rule quotes found in their source", "%", "exact", 0),
    "rules_shown_unlabelled": (
        "Rules",
        "Rules shown for a ticket that breaks none",
        "per ticket",
        "lower",
        0.5,
    ),
    # The learning loop (`learning.py`): what use teaches, and what it may not.
    "learning_probes_passed": (
        "Learning loop",
        "Learning loop probes passing",
        "%",
        "exact",
        0,
    ),
    # Speed - machine dependent, so a generous tolerance.
    "graph_p50_ms": ("Speed", "Graph answer, median", "ms", "lower", 15.0),
    "keyword_p50_ms": ("Speed", "Keyword answer, median", "ms", "lower", 15.0),
    "chunk_p50_ms": ("Speed", "Passage answer, median", "ms", "lower", 15.0),
    "graph_rows_p50_ms": ("Speed", "Graph answer from rows, median", "ms", "lower", 15.0),
}

HIPAA = [
    ("164.312(a)(1)", "Access control"),
    ("164.312(b)", "Audit controls"),
    ("164.312(c)(1)", "Integrity"),
    ("164.312(d)", "Person or entity authentication"),
    ("164.312(e)(1)", "Transmission security"),
    ("164.502(b)", "Minimum necessary"),
]


# ── the workspace ─────────────────────────────────────────────────────────────


def convert(path):
    """A document as Markdown, through the production converter, cached by content.

    None when the converter refuses one of the quality samples - a scanned page or
    an image with OCR off - which in production is a failed document and no source
    at all. Any other refusal is an error: a CarePath document that stopped
    converting must fail the scorecard, not quietly leave the corpus.
    """
    data = path.read_bytes()
    key = hashlib.sha256(data).hexdigest()
    cached = CACHE / f"{key}.md"
    if cached.is_file():
        return cached.read_text(encoding="utf-8")
    done = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "extract_text.py"), str(path), path.suffix.lower()],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=120,
        check=False,
    )
    if done.returncode != 0:
        if SAMPLES in path.parents:
            return None
        done.check_returncode()
    text = done.stdout.decode("utf-8").strip()
    CACHE.mkdir(parents=True, exist_ok=True)
    cached.write_text(text, encoding="utf-8")
    return text


def jira_payload():
    """The repository's Jira export, keyed and titled as the live demo site has them."""
    export = json.loads((KIT / "jira-export.json").read_text(encoding="utf-8"))
    # Frozen here, not read from a demo run's jira-created.json, which is gitignored
    # and names whatever keys the operator's own Jira handed out.
    created = json.loads((BENCH / "jira-keys.json").read_text(encoding="utf-8"))["issues"]
    issues = []
    for issue in export["issues"]:
        care = issue["key"]
        fields = dict(issue["fields"])
        fields["summary"] = f"{care} - {fields['summary']}"
        issues.append({**issue, "key": created.get(care, care), "fields": fields})
    return {"issues": issues}, {created.get(k, k): k for k in created}


@contextmanager
def stable_ids():
    """Sources get the same ids on every run, so ranking ties break the same way.

    Ids are random UUIDs, and ranking falls back on id order when two texts tie;
    with random ids a tie could fall either way between runs, and a gate that
    flips on a coin is no gate. Only the throwaway workspace is affected.
    """
    import itertools
    import uuid

    from .models import KnowledgeEntry

    field = KnowledgeEntry._meta.get_field("id")
    counter = itertools.count()
    had = "_get_default" in field.__dict__
    saved = field.__dict__.get("_get_default")
    field.__dict__["_get_default"] = lambda: uuid.uuid5(
        uuid.NAMESPACE_URL, f"scorecard/{next(counter)}"
    )
    try:
        yield
    finally:
        if had:
            field.__dict__["_get_default"] = saved
        else:
            del field.__dict__["_get_default"]


def workspace():
    """Organization, application, people and every source, in the throwaway database."""
    with stable_ids():
        return _workspace()


def _workspace():
    from .connector_kinds import MAX_BODY_CHARACTERS, jira_records
    from .models import (
        Application,
        ApplicationGrant,
        Organization,
        OrganizationMember,
        Portfolio,
        Product,
        User,
    )
    from .workbench import add_knowledge

    org = Organization.objects.create(name="Scorecard")
    product = Product.objects.create(
        name="Care", portfolio=Portfolio.objects.create(name="Care", organization=org)
    )
    app = Application.objects.create(name="CarePath scorecard", product=product)
    other = Application.objects.create(name="Elsewhere", product=product)
    people = {}
    for name, role in (("owner", "owner"), ("viewer", "viewer"), ("outsider", None)):
        user = User.objects.create_user(f"scorecard-{name}", email=f"{name}@scorecard.example")
        OrganizationMember.objects.create(organization=org, user=user, is_admin=name == "owner")
        if role:
            ApplicationGrant.objects.create(application=app, user=user, role=role)
        people[name] = user
    ApplicationGrant.objects.create(application=other, user=people["owner"], role="owner")
    owner = people["owner"]

    for path in sorted(p for p in DOCS.rglob("*") if p.is_file()):
        text = convert(path)
        if text is not None:
            add_knowledge(owner, app.pk, path.relative_to(DOCS).as_posix(), text)
    payload, care_keys = jira_payload()
    config = {"base_url": JIRA_SITE, "auth": "cloud", "account_email": "bot@scorecard.example"}
    with patch("platform_core.connector_kinds.api_json", return_value=payload):
        records = jira_records(config, "unused")
    tickets = {}
    for record in records:
        content = (record.title + "\n\n" + record.body)[:MAX_BODY_CHARACTERS]
        entry = add_knowledge(owner, app.pk, record.title, content, source=record.url)
        tickets[care_keys.get(record.external_id, record.external_id)] = entry
    return SimpleNamespace(app=app, other=other, people=people, tickets=tickets)


def publish(app):
    from django.db import transaction
    from django.utils import timezone

    from . import graph_store
    from .graphs import build_graph, sources_for
    from .models import GraphRevision

    entries = list(sources_for(app.pk))
    data, quality = build_graph(entries, len(entries))
    number = GraphRevision.objects.filter(application=app).count() + 1
    # Recorded as `graphs.rebuild` records a version, so answers from rows can
    # be held to the answers from the JSON (`graph_rows`).
    with transaction.atomic():
        revision = GraphRevision.objects.create(
            application=app,
            number=number,
            fingerprint=f"scorecard-{number}",
            data=data,
            quality=quality,
            published_at=timezone.now(),
        )
        graph_store.record(revision)
    return revision


# ── the aspects ───────────────────────────────────────────────────────────────


def ranks(sources, expected, entries):
    from .benchmark import matches

    for position, source_id in enumerate(sources, 1):
        entry = entries.get(source_id)
        if entry is not None and matches(entry, expected):
            return position
    return None


def retrieval(ws, revision, questions):
    from .graph_ai import citations_from, prepare
    from .models import KnowledgeEntry
    from .workbench import retrieve

    entries = {str(e.pk): e for e in KnowledgeEntry.objects.filter(application=ws.app)}
    ready = prepare(revision.data)
    owner = ws.people["owner"]
    result, details, cited_all = {}, [], []
    graph_ranks, keyword_ranks, chunk_ranks = [], [], []
    for question in questions:
        cited = citations_from(
            ws.app.pk, revision.data, revision.number, question["question"], ready, viewer=owner
        )
        cited_all.extend(cited)
        graph_ranks.append(
            ranks(list(dict.fromkeys(c["id"] for c in cited)), question["expected"], entries)
        )
        found = retrieve(ws.app, question["question"], owner, candidates="sources")
        keyword_ranks.append(
            ranks(
                list(dict.fromkeys(str(e.pk) for _, e, _ in found)), question["expected"], entries
            )
        )
        found = retrieve(ws.app, question["question"], owner, candidates="chunks")
        chunk_ranks.append(
            ranks(
                list(dict.fromkeys(str(e.pk) for _, e, _ in found)), question["expected"], entries
            )
        )
        details.append(
            {
                "question": question["question"],
                "expected": question["expected"],
                "graph_rank": graph_ranks[-1],
                "keyword_rank": keyword_ranks[-1],
                "chunk_rank": chunk_ranks[-1],
            }
        )
    for prefix, values in (
        ("graph", graph_ranks),
        ("keyword", keyword_ranks),
        ("chunk", chunk_ranks),
    ):
        result[f"{prefix}_found"] = sum(1 for r in values if r)
        result[f"{prefix}_top3"] = sum(1 for r in values if r and r <= 3)
        result[f"{prefix}_mrr"] = round(sum(1 / r for r in values if r) / len(values), 3)
    return result, details, cited_all


def integrity(ws, revision, cited_all):
    """Every citation re-checked from scratch; then a source changes under one."""
    from .graph_ai import citations_from
    from .models import KnowledgeEntry

    live = {str(e.pk): e for e in KnowledgeEntry.objects.filter(application=ws.app)}
    ok = sum(
        1
        for c in cited_all
        if c["id"] in live
        and live[c["id"]].digest == c["digest"]
        and hashlib.sha256(live[c["id"]].content.encode()).hexdigest() == c["digest"]
        and c["quote"] in live[c["id"]].content
    )
    verified = round(100 * ok / len(cited_all), 1) if cited_all else 100.0
    # Re-import one cited source with its quoted text gone, as an edit would.
    target = live[cited_all[0]["id"]]
    question = "Is patient consent checked before a FHIR export?"
    before = {
        c["id"]
        for c in citations_from(
            ws.app.pk, revision.data, revision.number, question, viewer=ws.people["owner"]
        )
    }
    changed = target.content.replace(cited_all[0]["quote"], "[changed]")
    KnowledgeEntry.objects.filter(pk=target.pk).update(
        content=changed, digest=hashlib.sha256(changed.encode()).hexdigest()
    )
    after = citations_from(
        ws.app.pk, revision.data, revision.number, question, viewer=ws.people["owner"]
    )
    stale = sum(
        1 for c in after if c["id"] == str(target.pk) and c["quote"] == cited_all[0]["quote"]
    )
    KnowledgeEntry.objects.filter(pk=target.pk).update(content=target.content, digest=target.digest)
    return {"citations_verified": verified, "stale_citations": stale}, {
        "changed_source": target.title,
        "was_cited": str(target.pk) in before,
    }


def ticket_question(ticket):
    """A ticket asked as Code Factory asks it: its title and description, not its header."""
    from .serviceops import fields_and_description

    _, description = fields_and_description(ticket)
    return (ticket.title + "\n" + description)[:4000]


def agentic(ws, revision):
    """What Code Factory is handed for each ticket with a planted bug, scored by the answer key."""
    from .graph_ai import MAX_REFERENCED, citations_from

    key = json.loads((BENCH / "seeded-evidence.json").read_text(encoding="utf-8"))["tickets"]
    found = expected = self_cited = 0
    details = []
    for care, identifiers in key.items():
        ticket = ws.tickets[care]
        question = ticket_question(ticket)
        # As `code_factory.evidence_for` asks: the eight, then cross-references.
        cited = citations_from(
            ws.app.pk,
            revision.data,
            revision.number,
            question,
            viewer=ws.people["owner"],
            references=MAX_REFERENCED,
        )
        text = "\n".join(f"{c['title']}\n{c['excerpt']}" for c in cited)
        hits = [i for i in identifiers if re.search(rf"(?<![\w-]){re.escape(i)}(?![\w-])", text)]
        found += len(hits)
        expected += len(identifiers)
        self_cited += any(c["id"] == str(ticket.pk) for c in cited)
        details.append({"ticket": care, "expected": identifiers, "found": hits})
    return {
        "seeded_evidence_recall": round(100 * found / expected, 1),
        "seeded_tickets_self_cited": self_cited,
    }, details


def graph_rows(ws, revision, questions):
    """Answers from the version's rows against answers from its JSON, and their speed.

    Every benchmark question as chat asks it, and every seeded ticket as Code
    Factory asks it, with cross-references: the rows must give the same
    citations in the same order, or switching `graph_candidates` would change
    what people are told.
    """
    from .graph_ai import MAX_REFERENCED, citations_from, prepare
    from .graph_store import RowsGraph

    ready = prepare(revision.data)
    owner = ws.people["owner"]
    asked = [(q["question"], 0) for q in questions] + [
        (ticket_question(ws.tickets[care]), MAX_REFERENCED)
        for care in json.loads((BENCH / "seeded-evidence.json").read_text(encoding="utf-8"))[
            "tickets"
        ]
    ]
    same, timings, differ = 0, [], []
    for question, references in asked:
        memory = citations_from(
            ws.app.pk,
            revision.data,
            revision.number,
            question,
            ready,
            viewer=owner,
            references=references,
        )
        start = time.perf_counter()
        rows = citations_from(
            ws.app.pk,
            None,
            revision.number,
            question,
            viewer=owner,
            references=references,
            graph=RowsGraph(revision),
        )
        timings.append((time.perf_counter() - start) * 1000)
        if rows == memory:
            same += 1
        else:
            differ.append(question[:80])
    return {
        "graph_rows_same": round(100 * same / len(asked), 1),
        "graph_rows_p50_ms": round(statistics.median(timings), 1),
    }, {"asked": len(asked), "different": differ}


def ontology_quality():
    """Typing and merging against labelled cases; no database, no model."""
    from .graph_ai import EXTRACTION_INSTRUCTIONS
    from .ontology import RELATIONS, TYPES, merge_entities, typed

    samples = json.loads((BENCH / "ontology-samples.json").read_text(encoding="utf-8"))
    right, wrong = 0, []
    for case in samples["relations"]:
        got = typed(case["subject"], case["relation"], case["object"])
        got = got[1] if got else None
        if got == case["expected"]:
            right += 1
        else:
            wrong.append({**case, "got": got})
    named = [*TYPES, *(f"'{name}'" for name in RELATIONS)]
    stated = sum(name in EXTRACTION_INSTRUCTIONS for name in named)

    merged_right = merged_wrong = missed = 0
    merge_errors = []
    for case in samples["merges"]:
        data = {
            "nodes": [
                {
                    "id": "a",
                    "kind": "entity",
                    "type": case["type"],
                    "label": case["a"],
                    "knowledge_id": "s1",
                },
                {
                    "id": "b",
                    "kind": "entity",
                    "type": case.get("type_b", case["type"]),
                    "label": case["b"],
                    "knowledge_id": "s2",
                },
            ],
            "edges": [],
        }
        did = merge_entities("scorecard", data) > 0
        if did and case["merge"]:
            merged_right += 1
        elif did:
            merged_wrong += 1
            merge_errors.append(case)
        elif case["merge"]:
            missed += 1
            merge_errors.append(case)
    should = sum(1 for case in samples["merges"] if case["merge"])
    return {
        "ontology_typing_accuracy": round(100 * right / len(samples["relations"]), 1),
        "ontology_prompt_coverage": round(100 * stated / len(named), 1),
        "merge_precision": round(100 * merged_right / (merged_right + merged_wrong), 1)
        if merged_right + merged_wrong
        else 100.0,
        "merge_recall": round(100 * merged_right / should, 1) if should else 100.0,
    }, {"typing_errors": wrong, "merge_errors": merge_errors}


def document_typing():
    """Every document typed by `document_types`, against benchmarks/document-types.json.

    Twice: as uploaded, with its file name (not its folder, which says the answer),
    and with its title line and file name removed, which is how well the type is
    read from what the document contains. No database, no model.
    """
    from .document_types import classify, parse

    labels = json.loads((BENCH / "document-types.json").read_text(encoding="utf-8"))
    labels = labels["documents"]
    right = untitled = typed = 0
    wrong = []
    for path in sorted(p for p in DOCS.rglob("*") if p.is_file()):
        text = convert(path)
        if text is None:
            continue
        name = path.relative_to(DOCS).as_posix()
        # A document nobody labelled fails the metric rather than leaving it.
        expected = labels.get(name, "unlabelled")
        got = classify(text, path.name)["key"]
        title_line = parse(text).title_line
        lines = text.splitlines()
        bare = "\n".join(lines[: title_line - 1] + lines[title_line:]) if title_line else text
        got_bare = classify(bare)["key"]
        typed += 1
        right += got == expected
        untitled += got_bare == expected
        if got != expected or got_bare != expected:
            wrong.append(
                {"document": name, "expected": expected, "got": got, "got_untitled": got_bare}
            )
    return {
        "document_type_accuracy": round(100 * right / typed, 1) if typed else 0.0,
        "document_type_untitled_accuracy": round(100 * untitled / typed, 1) if typed else 0.0,
    }, {"typed": typed, "mistyped": wrong}


def personal_data(ws, revision):
    """The scan against labelled samples, then restriction and permissions on every path."""
    from types import SimpleNamespace as Scope

    from .agent_runtime.tools import NOT_FOUND, fetch_source, verify_citations
    from .code_factory import ticket_choices
    from .graph_ai import citations_from
    from .models import KnowledgeEntry
    from .sensitivity import RESTRICTED, scan
    from .workbench import retrieve

    samples = json.loads((BENCH / "phi-samples.json").read_text(encoding="utf-8"))["samples"]
    true_pos = false_pos = false_neg = 0
    misses = []
    for sample in samples:
        got, want = set(scan(sample["text"])), set(sample["kinds"])
        true_pos += len(got & want)
        false_pos += len(got - want)
        false_neg += len(want - got)
        if got != want:
            misses.append({"text": sample["text"], "expected": sorted(want), "found": sorted(got)})
    recall = round(100 * true_pos / (true_pos + false_neg), 1) if true_pos + false_neg else 100.0
    precision = round(100 * true_pos / (true_pos + false_pos), 1) if true_pos + false_pos else 100.0

    owner, viewer = ws.people["owner"], ws.people["viewer"]
    questions = [
        "Is patient consent checked before a FHIR export?",
        "Which log line leaks the patient name and date of birth?",
        "How is login protected against credential stuffing?",
    ]

    def reachable(user):
        seen = set()
        for q in questions:
            seen |= {
                c["id"]
                for c in citations_from(ws.app.pk, revision.data, revision.number, q, viewer=user)
            }
            seen |= {str(e.pk) for _, e, _ in retrieve(ws.app, q, user)}
        return seen

    # Restrict three cited sources, then try every path that hands a source to AI.
    restricted = sorted(reachable(owner))[:3]
    KnowledgeEntry.objects.filter(pk__in=restricted).update(sensitivity=RESTRICTED)
    leaks = len(reachable(owner) & set(restricted))
    scope = Scope(user_id=owner.pk, app_id=ws.app.pk)
    for entry in KnowledgeEntry.objects.filter(pk__in=restricted):
        leaks += fetch_source(scope, {"source_id": str(entry.pk)}).text != NOT_FOUND
        leaks += bool(
            verify_citations(
                ws.app.pk,
                [{"id": str(entry.pk), "digest": entry.digest, "excerpt": entry.content[:40]}],
                owner,
            )
        )
    leaks += sum(str(e.pk) in restricted for e in ticket_choices(ws.app, viewer=owner))
    KnowledgeEntry.objects.filter(pk__in=restricted).update(sensitivity="standard")

    # Limit two tickets to the owner, as an enforcing connector would.
    limited = [ws.tickets["CARE-401"], ws.tickets["CARE-418"]]
    KnowledgeEntry.objects.filter(pk__in=[t.pk for t in limited]).update(
        access_principals=[str(owner.pk)]
    )
    ids = {str(t.pk) for t in limited}
    permission = len(reachable(viewer) & ids) + len(reachable(None) & ids)
    permission += sum(str(e.pk) in ids for e in ticket_choices(ws.app, viewer=viewer))
    owner_still_sees = bool(reachable(owner) & ids)
    KnowledgeEntry.objects.filter(pk__in=[t.pk for t in limited]).update(access_principals=None)
    return {
        "phi_recall": recall,
        "phi_precision": precision,
        "restricted_leaks": leaks,
        "permission_leaks": permission,
    }, {
        "phi_misses": misses,
        "owner_still_sees_limited": owner_still_sees,
        "restricted_sources": len(restricted),
    }


def hipaa(ws, revision, integrity_result, data_result):
    """Probes for the HIPAA technical safeguards, each pass or fail."""
    from django.conf import settings
    from django.core.exceptions import PermissionDenied
    from django.http import Http404
    from django.test import Client

    from .agent_runtime.tools import NOT_FOUND, fetch_source
    from .models import AuditEvent, KnowledgeEntry
    from .policy import application_for
    from .workbench import add_knowledge

    probes = []

    def probe(control, name, check):
        try:
            passed = bool(check())
        except Exception:  # noqa: BLE001 - a probe that errors has failed
            passed = False
        probes.append({"control": control, "probe": name, "passed": passed})

    def refuses(fn, *errors):
        try:
            fn()
        except errors:
            return True
        return False

    outsider, viewer, owner = ws.people["outsider"], ws.people["viewer"], ws.people["owner"]
    foreign = KnowledgeEntry.objects.filter(application=ws.app).first()
    probe(
        "164.312(a)(1)",
        "No grant reads as not found",
        lambda: refuses(lambda: application_for(outsider, ws.app.pk), Http404),
    )
    probe(
        "164.312(a)(1)",
        "A viewer cannot add knowledge",
        lambda: refuses(
            lambda: add_knowledge(viewer, ws.app.pk, "x", "y"), PermissionDenied, Http404
        ),
    )
    probe(
        "164.312(a)(1)",
        "Another application's source reads as not found",
        lambda: (
            fetch_source(
                SimpleNamespace(user_id=owner.pk, app_id=ws.other.pk),
                {"source_id": str(foreign.pk)},
            ).text
            == NOT_FOUND
        ),
    )
    probe(
        "164.312(b)",
        "Every source added is audited",
        lambda: (
            AuditEvent.objects.filter(action="knowledge.created").count()
            >= KnowledgeEntry.objects.filter(application=ws.app).count()
        ),
    )
    probe(
        "164.312(c)(1)",
        "Citations re-verify against their source",
        lambda: integrity_result["citations_verified"] == 100.0,
    )
    probe(
        "164.312(c)(1)",
        "A changed source is no longer cited",
        lambda: integrity_result["stale_citations"] == 0,
    )
    probe(
        "164.312(d)",
        "Passwords hashed with Argon2",
        lambda: "Argon2" in settings.PASSWORD_HASHERS[0],
    )
    probe(
        "164.312(d)", "Repeated failed sign-ins lock out", lambda: "axes" in settings.INSTALLED_APPS
    )
    probe(
        "164.312(d)",
        "Sessions expire within 30 minutes",
        lambda: settings.SESSION_COOKIE_AGE <= 1800,
    )
    probe(
        "164.312(e)(1)", "Session cookies are HTTP-only", lambda: settings.SESSION_COOKIE_HTTPONLY
    )

    def strict_csp():
        response = Client().get("/accounts/login/")
        policy = response.headers.get("Content-Security-Policy", "")
        # The platform's own policy is stricter than 'self': nothing by default.
        return (
            re.search(r"default-src '(none|self)'", policy) is not None
            and "script-src 'self'" in policy
            and "unsafe-inline" not in policy
            and "unsafe-eval" not in policy
        )

    probe("164.312(e)(1)", "Strict content security policy", strict_csp)
    probe(
        "164.502(b)",
        "Restricted sources never reach AI",
        lambda: data_result["restricted_leaks"] == 0,
    )
    probe(
        "164.502(b)",
        "Records limited by their source system stay limited",
        lambda: data_result["permission_leaks"] == 0,
    )
    passed = sum(p["passed"] for p in probes)
    return {"hipaa_controls_passed": round(100 * passed / len(probes), 1)}, probes


def cost_control(ws):
    """AI extraction calls on reruns, and the budget, with the model replaced."""
    from django.db import transaction

    from . import graph_store
    from .budget import BudgetExceeded
    from .graph_ai import enrich_graph
    from .graphs import build_graph, sources_for
    from .models import (
        AIBudget,
        AIConfiguration,
        AIUsage,
        GraphElement,
        GraphRevision,
        KnowledgeEntry,
    )

    owner = ws.people["owner"]
    config = AIConfiguration.objects.create(
        application=ws.app,
        purpose="graph_generation",
        provider="claude",
        model="claude-sonnet-5",
        input_rate=Decimal("2"),
        output_rate=Decimal("10"),
        configured_by=owner,
    )
    calls, versions = [], []

    def extract(user, app_id, purpose, question, citations, **options):
        calls.append(citations[0]["id"])
        return json.dumps({"relationships": []})

    def run():
        entries = list(sources_for(ws.app.pk))
        data, quality = build_graph(entries, len(entries))
        before = len(calls)
        with patch("platform_core.ai.invoke_ai", side_effect=extract):
            data, quality = enrich_graph(ws.app.pk, config, entries, data, quality)
        # Recorded as `graphs.rebuild` records a version, so the graph storage
        # figures measure the versions a run of rebuilds actually leaves.
        with transaction.atomic():
            revision = GraphRevision.objects.create(
                application=ws.app,
                number=GraphRevision.objects.filter(application=ws.app).count() + 1,
                fingerprint="scorecard-ai",
                data=data,
                quality=quality,
                provider="claude",
                model="claude-sonnet-5",
            )
            graph_store.record(revision)
        versions.append(revision)
        return len(calls) - before

    first = run()
    # Work through the library until every source has been extracted once.
    while first and run():
        pass
    unchanged = run()
    entry = KnowledgeEntry.objects.filter(application=ws.app).order_by("id").first()
    changed = entry.content + "\nEdited."
    KnowledgeEntry.objects.filter(pk=entry.pk).update(
        content=changed, digest=hashlib.sha256(changed.encode()).hexdigest()
    )
    one_change = run()
    changed_version = versions[-1]
    written = GraphElement.objects.filter(
        application=ws.app, first_number=changed_version.number
    ).count()
    elements = sum(len(changed_version.data.get(name, [])) for name in graph_store.ELEMENTS)
    exact = sum(graph_store.verify(version) for version in versions)

    AIBudget.objects.create(application=ws.app, monthly_limit=Decimal("1"))
    AIUsage.objects.create(
        application=ws.app,
        actor=owner,
        provider="Anthropic",
        model="claude-sonnet-5",
        request_id="scorecard-spend",
        input_tokens=1,
        output_tokens=1,
        amount=Decimal("1"),
        estimated=True,
    )
    over = 0
    with (
        patch("platform_core.claude_agents.completion") as provider,
        patch("platform_core.secrets.application_secret", return_value="unused"),
        # "unused" reads as an API key; the provider patched is the CLI's.
        override_settings(CLAUDE_MESSAGES_API=False),
    ):
        try:
            from .ai import invoke_ai

            invoke_ai(
                owner,
                ws.app.pk,
                "graph_generation",
                "q",
                [{"id": "x", "title": "t", "excerpt": "e"}],
            )
        except BudgetExceeded:
            pass
        over = provider.call_count
    past = gateway_held(ws, owner)
    return {
        "ai_calls_unchanged_rerun": unchanged,
        "ai_calls_one_change": one_change,
        "paid_calls_over_budget": over,
        "paid_calls_past_gateway": past,
        "graph_rows_exact": round(100 * exact / len(versions), 1),
        "graph_rows_one_change": round(100 * written / max(1, elements), 1),
    }, {"first_run_calls": first, "graph_versions": len(versions), "rows_written": written}


def gateway_held(ws, owner):
    """Provider calls started that the gateway should have held back.

    Two moments: under the cap by receipts, but not once a call in flight's
    reservation counts; and every slot this application may hold taken. Both
    must refuse before the provider is reached, with the model replaced.
    """
    from datetime import timedelta

    from django.conf import settings
    from django.test.utils import override_settings
    from django.utils import timezone

    from . import gateway
    from .ai import invoke_ai
    from .budget import BudgetExceeded
    from .models import AIAdmission, AIBudget

    AIBudget.objects.filter(application=ws.app).update(monthly_limit=Decimal("2"))
    citations = [{"id": "x", "title": "t", "excerpt": "e"}]

    def hold(reserved):
        return AIAdmission.objects.create(
            application_id=ws.app.pk,
            purpose="graph_generation",
            provider="claude",
            actor=owner,
            reserved=Decimal(reserved),
            expires_at=timezone.now() + timedelta(minutes=5),
        )

    started = 0
    with (
        patch("platform_core.claude_agents.completion") as provider,
        patch("platform_core.secrets.application_secret", return_value="unused"),
        # "unused" reads as an API key; the provider patched is the CLI's.
        override_settings(CLAUDE_MESSAGES_API=False),
        patch.object(gateway, "logger"),
    ):
        hold("1.5")
        try:
            invoke_ai(owner, ws.app.pk, "graph_generation", "q", citations)
        except BudgetExceeded:
            pass
        AIAdmission.objects.all().delete()
        with override_settings(
            AI_GATEWAY={**settings.AI_GATEWAY, "max_calls_per_application": 2,
                        "slot_wait_seconds": 0}
        ):
            hold("0")
            hold("0")
            try:
                invoke_ai(owner, ws.app.pk, "graph_generation", "q", citations)
            except gateway.ProviderBusy:
                pass
        AIAdmission.objects.all().delete()
        started = provider.call_count
    return started


def release_check(ws, revision):
    """Whether the release check refuses, warns and passes as designed, against the
    published scorecard version. Inside a savepoint that is rolled back, so the
    aspects after it read the workspace as it was."""
    from django.core.exceptions import ValidationError
    from django.db import transaction

    from .graphs import build_graph, sources_for
    from .models import BenchmarkRun, GraphRevision, KnowledgeEntry
    from .release import check, publish

    def version(entries, edges=()):
        data, quality = build_graph(entries, len(entries))
        data["edges"] = data.get("edges", []) + list(edges)
        return GraphRevision.objects.create(
            application=ws.app,
            number=GraphRevision.objects.filter(application=ws.app).count() + 1,
            fingerprint="scorecard-release",
            data=data,
            quality=quality,
        )

    def refused(number, reason):
        try:
            publish(ws.people["owner"], ws.app, number, reason)
        except ValidationError:
            return True
        return False

    probes = {}
    with transaction.atomic():
        entries = list(sources_for(ws.app.pk))
        sound = version(entries)
        probes["an unchanged rebuild passes"] = check(ws.app, sound, revision).passed

        lost = version(entries[1:])
        report = check(ws.app, lost, revision)
        probes["a lost source warns, and nothing else does"] = (
            report.publishable and [f.key for f in report.soft_failures] == ["sources_lost"]
        )
        probes["a warning without a reason is refused"] = refused(lost.number, "")

        first = entries[0]
        tampered = version(entries, [{
            "source": "entity:scorecard:a",
            "target": "entity:scorecard:b",
            "relation": "depends on",
            "knowledge_id": str(first.pk),
            "digest": first.digest,
            "evidence": "A sentence no source in the library says.",
            "inferred": True,
        }])
        probes["a quote its source does not hold is refused"] = (
            [f.key for f in check(ws.app, tampered, revision).hard_failures] == ["citations"]
            and refused(tampered.number, "Anyway")
        )

        for run, found in ((revision, 16), (sound, 15)):
            BenchmarkRun.objects.create(
                application=ws.app, revision=run, questions=16, found=found,
                top_three=found, reciprocal_rank=found / 16,
            )
        probes["a benchmark regression warns"] = [
            f.key for f in check(ws.app, sound, revision).soft_failures
        ] == ["benchmark"]

        restricted = version(entries)
        KnowledgeEntry.objects.filter(pk=first.pk).update(sensitivity="restricted")
        probes["a restricted source is refused"] = "restricted" in [
            f.key for f in check(ws.app, restricted, revision).hard_failures
        ]
        KnowledgeEntry.objects.filter(pk=first.pk).update(sensitivity="standard")

        published, _ = publish(ws.people["owner"], ws.app, lost.number, "Retired on purpose")
        probes["a reason given is kept on the version"] = (
            published.published_at is not None
            and published.publish_reason == "Retired on purpose"
        )

        # Automatic publishing: never unless an owner said so, then only a
        # version that passes everything and is scored.
        from .release import auto_publish, set_auto_publish

        whole = version(entries)
        BenchmarkRun.objects.create(
            application=ws.app, revision=whole, questions=16, found=16, top_three=16,
            reciprocal_rank=1.0,
        )
        probes["a passing version waits for a person by default"] = (
            auto_publish(whole, check(ws.app, whole)) is None
        )
        set_auto_publish(ws.people["owner"], ws.app, True)
        probes["once allowed, a passing scored version publishes itself"] = (
            auto_publish(whole, check(ws.app, whole)) is not None
        )
        transaction.set_rollback(True)
    passed = sum(bool(ok) for ok in probes.values())
    return {"release_probes_passed": round(100 * passed / len(probes), 1)}, probes


def application_rules(ws):
    """Whether the rules a ticket breaks are the rules its plan review shows.

    The pack names a rules document and, per seeded ticket, the statement it
    breaks. Inside a rolled-back savepoint the document is marked as rules, and
    each ticket is asked as `rules.for_run` asks it, without a plan. Every quote
    shown is checked against its source's text.
    """
    from django.db import transaction

    from .models import KnowledgeEntry
    from .rules import SHOWN, applicable, sources

    pack = json.loads((BENCH / "rules-pack.json").read_text(encoding="utf-8"))
    shown = first = verified = quotes = noise = 0
    details = []
    with transaction.atomic():
        KnowledgeEntry.objects.filter(
            application=ws.app, active=True, title__in=pack["rule_sources"]
        ).update(rules_by=ws.people["owner"])
        entries = list(sources(ws.app))
        texts = {str(entry.pk): entry.content.splitlines() for entry in entries}
        for care in list(pack["tickets"]) + pack["unlabelled"]:
            found = applicable(ws.app, ticket_question(ws.tickets[care]), SHOWN, entries)
            statements = [rule.statement for rule, _ in found]
            for rule, _ in found:
                quotes += 1
                lines = texts[rule.entry_id]
                verified += 0 < rule.line <= len(lines) and lines[rule.line - 1] == rule.quote
            expected = pack["tickets"].get(care)
            if expected:
                shown += expected in statements
                first += bool(statements) and statements[0] == expected
            else:
                noise += len(statements)
            details.append({"ticket": care, "expected": expected, "shown": statements})
        transaction.set_rollback(True)
    labelled = len(pack["tickets"])
    return {
        "rules_shown_recall": round(100 * shown / labelled, 1),
        "rules_first_recall": round(100 * first / labelled, 1),
        "rules_quotes_verified": round(100 * verified / quotes, 1) if quotes else 100.0,
        "rules_shown_unlabelled": round(noise / len(pack["unlabelled"]), 2),
    }, details


def learning_loop(ws, revision):
    """Whether the learning loop offers, refuses and withdraws as designed. Inside
    a savepoint that is rolled back, so nothing it learns reaches the aspects
    after it - and none of it changes what the scorecard measures before it."""
    from django.core.exceptions import PermissionDenied
    from django.db import transaction
    from django.utils import timezone

    from . import learning
    from .code_knowledge import LEARNED_PREFIX, references
    from .models import (
        ChangePlan,
        ChatConversation,
        ChatMessage,
        FactoryRun,
        GraphRevision,
        KnowledgeEntry,
        LearningCandidate,
        ProposedChange,
        TriageHypothesis,
        TriageRun,
        TriageVerdict,
    )

    owner, viewer = ws.people["owner"], ws.people["viewer"]
    probes = {}
    with transaction.atomic():
        plan = ChangePlan.objects.create(
            application=ws.app, author=owner, title="Fix", proposal="p", validation="v",
            digest="d", status="approved", reviewed_by=owner, reviewed_at=timezone.now(),
        )
        run = FactoryRun.objects.create(
            application=ws.app, requested_by=owner, number=900, ticket_external_id="CARE-401",
            ticket_title="Scorecard change", plan=plan, status="delivered",
            pull_request_url="https://github.com/example/carepath/pull/1",
            checks=[{"name": "tests", "conclusion": "success"}],
        )
        ProposedChange.objects.create(run=run, path="carepath/export.py", content="x")
        failing = FactoryRun.objects.create(
            application=ws.app, requested_by=owner, number=901, plan=plan, status="delivered",
            pull_request_url="https://github.com/example/carepath/pull/2",
            checks=[{"name": "tests", "conclusion": "failure"}],
        )
        incident, cause = list(KnowledgeEntry.objects.filter(application=ws.app, active=True)[:2])
        triage = TriageRun.objects.create(
            application=ws.app, incident=incident, requested_by=owner,
            incident_digest=incident.digest, pack_digest="p",
        )
        verdict = TriageVerdict.objects.create(
            hypothesis=TriageHypothesis.objects.create(
                run=triage, rank=1, statement="s", next_step="n",
                citations=[{"id": str(cause.pk), "quote": "q"}],
            ),
            user=owner, verdict="accepted", actual_cause=cause, confirmed_at=timezone.now(),
        )
        conversation = ChatConversation.objects.create(application=ws.app, user=viewer, title="t")
        for number, question in enumerate(
            ("Which colour is the scorecard's lobby?", "Where is MRN: 4471234 treated?")
        ):
            ChatMessage.objects.create(
                application=ws.app, user=viewer, conversation=conversation, role="user",
                body=question, sequence=2 * number + 1,
            )
            ChatMessage.objects.create(
                application=ws.app, user=viewer, conversation=conversation, role="assistant",
                body="Nothing found.", sequence=2 * number + 2,
            )

        sources = KnowledgeEntry.objects.count()
        learning.collect(ws.app)
        offered = LearningCandidate.objects.filter(application=ws.app)
        probes["it offers each signal and makes no knowledge"] = (
            offered.filter(state="open").count() == 3 and KnowledgeEntry.objects.count() == sources
        )
        probes["a failing delivery is not offered"] = not offered.filter(run=failing).exists()
        probes["a question with personal data is not copied"] = not offered.filter(
            question__contains="4471234"
        ).exists() and not offered.filter(title__contains="4471234").exists()

        change = offered.get(run=run)
        try:
            learning.accept(viewer, ws.app.pk, change.pk)
            probes["a viewer cannot accept"] = False
        except PermissionDenied:
            probes["a viewer cannot accept"] = True
        entry = learning.accept(owner, ws.app.pk, change.pk).entry
        derived = GraphRevision(
            application=ws.app, number=0, data={"edges": [{
                "knowledge_id": str(entry.pk), "digest": entry.digest, "line": 1,
                "evidence": entry.content.splitlines()[0], "source": "a", "target": "b",
            }]},
        )
        probes["an accepted change record is never requirement evidence"] = (
            entry.source.startswith(LEARNED_PREFIX) and not list(references(derived))
        )

        gap = offered.get(kind="gap", state="open")
        learning.reject(owner, ws.app.pk, gap.pk)
        probes["a rejection is not offered again"] = learning.collect(ws.app) == 0

        learned = learning.accept(owner, ws.app.pk, offered.get(verdict=verdict).pk).entry
        verdict.delete()
        learning.withdraw(ws.app)
        learned.refresh_from_db()
        probes["a retracted verdict retires its learning"] = not learned.active
        transaction.set_rollback(True)
    passed = sum(bool(ok) for ok in probes.values())
    return {"learning_probes_passed": round(100 * passed / len(probes), 1)}, probes


def speed(ws, revision, questions):
    from .graph_ai import citations_from, prepare
    from .workbench import retrieve

    ready = prepare(revision.data)
    owner = ws.people["owner"]
    graph, keyword, chunk = [], [], []
    for _ in range(5):
        for question in questions:
            start = time.perf_counter()
            citations_from(
                ws.app.pk, revision.data, revision.number, question["question"], ready, viewer=owner
            )
            graph.append((time.perf_counter() - start) * 1000)
            start = time.perf_counter()
            retrieve(ws.app, question["question"], owner, candidates="sources")
            keyword.append((time.perf_counter() - start) * 1000)
            start = time.perf_counter()
            retrieve(ws.app, question["question"], owner, candidates="chunks")
            chunk.append((time.perf_counter() - start) * 1000)
    return {
        "graph_p50_ms": round(statistics.median(graph), 1),
        "keyword_p50_ms": round(statistics.median(keyword), 1),
        "chunk_p50_ms": round(statistics.median(chunk), 1),
    }, {}


# ── running it ────────────────────────────────────────────────────────────────


@contextmanager
def throwaway_database():
    """A test database of its own, as `manage.py test` makes, removed afterwards."""
    from django.test.runner import DiscoverRunner
    from django.test.utils import setup_test_environment, teardown_test_environment

    setup_test_environment()
    runner = DiscoverRunner(verbosity=0, interactive=False)
    old = runner.setup_databases()
    try:
        yield
    finally:
        runner.teardown_databases(old)
        teardown_test_environment()


def commit():
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def measure():
    """Every aspect, in a throwaway database. Returns the scorecard as plain data."""
    from django.utils import timezone

    questions = json.loads((KIT / "benchmark-questions.json").read_text(encoding="utf-8"))
    with throwaway_database(), patch("platform_core.embeddings.configuration", return_value=None):
        ws = workspace()
        revision = publish(ws.app)
        values, details = {}, {}
        found, details["questions"], cited_all = retrieval(ws, revision, questions)
        values.update(found)
        found, details["integrity"] = integrity(ws, revision, cited_all)
        values.update(found)
        found, details["seeded_tickets"] = agentic(ws, revision)
        values.update(found)
        found, details["graph_rows"] = graph_rows(ws, revision, questions)
        values.update(found)
        found, details["ontology"] = ontology_quality()
        values.update(found)
        found, details["document_types"] = document_typing()
        values.update(found)
        data, details["personal_data"] = personal_data(ws, revision)
        values.update(data)
        found, details["hipaa"] = hipaa(ws, revision, values, data)
        values.update(found)
        found, details["release_check"] = release_check(ws, revision)
        values.update(found)
        found, details["learning_loop"] = learning_loop(ws, revision)
        values.update(found)
        found, details["rules"] = application_rules(ws)
        values.update(found)
        # Timed before cost control, which edits a source and adds versions.
        found, _ = speed(ws, revision, questions)
        values.update(found)
        found, details["cost"] = cost_control(ws)
        values.update(found)
        sources = len(revision.data.get("sources", []))
    return {
        "generated_at": timezone.now().isoformat(timespec="seconds"),
        "commit": commit(),
        "corpus": {"sources": sources, "questions": len(questions)},
        "metrics": {
            name: {
                "aspect": aspect,
                "label": label,
                "unit": unit,
                "better": better,
                "tolerance": tolerance,
                "value": values[name],
            }
            for name, (aspect, label, unit, better, tolerance) in METRICS.items()
        },
        "details": details,
    }


def regressions(card, baseline):
    """Metrics that moved the wrong way past their tolerance: [(name, was, now)]."""
    found = []
    for name, metric in card["metrics"].items():
        was = baseline.get("metrics", {}).get(name, {}).get("value")
        if was is None:
            continue
        now, tolerance = metric["value"], metric["tolerance"]
        worse = {
            "higher": now < was - tolerance,
            "lower": now > was + tolerance,
            "exact": now != was,
        }[metric["better"]]
        if worse:
            found.append((name, was, now))
    return found


# ── replaying an earlier commit ───────────────────────────────────────────────


def _at(commit, path):
    return subprocess.run(
        ["git", "show", f"{commit}:{path}"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout


#: What did not exist yet at the commit `replay` is pointed at by default, the
#: one before the retrieval roadmap. Everything else existed and simply is not
#: re-run against old code.
NOT_REPLAYED = {
    **dict.fromkeys(
        [
            "ontology_typing_accuracy",
            "ontology_prompt_coverage",
            "merge_precision",
            "merge_recall",
            "phi_recall",
            "phi_precision",
            "restricted_leaks",
            "permission_leaks",
            "ai_calls_unchanged_rerun",
            "ai_calls_one_change",
            "paid_calls_over_budget",
            "paid_calls_past_gateway",
        ],
        "not built yet",
    ),
}


def replay(commit):
    """Retrieval and speed as an earlier commit's code ranked them, on today's corpus.

    Only the ranking code is taken from that commit - its `graph_ai` module and
    its `workbench.retrieve` - so the same corpus, questions and judging apply
    and the numbers compare. Aspects that commit did not have yet are recorded
    as not built rather than guessed.
    """
    import ast
    import importlib.util
    import tempfile

    from django.db.models import Q
    from django.utils import timezone

    from .models import KnowledgeEntry

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / f"graph_ai_{commit}.py"
        path.write_text(_at(commit, "platform_core/graph_ai.py"), encoding="utf-8")
        spec = importlib.util.spec_from_file_location(f"platform_core.graph_ai_{commit}", path)
        old_graph = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = old_graph
        spec.loader.exec_module(old_graph)
    source = _at(commit, "platform_core/workbench.py")
    tree = ast.parse(source)
    retrieve_def = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "retrieve"
    )
    namespace = {"re": re, "Q": Q, "KnowledgeEntry": KnowledgeEntry}
    exec(compile(ast.Module([retrieve_def], []), f"{commit}:workbench.py", "exec"), namespace)
    old_retrieve = namespace["retrieve"]

    questions = json.loads((KIT / "benchmark-questions.json").read_text(encoding="utf-8"))
    values = {}
    with throwaway_database():
        ws = workspace()
        revision = publish(ws.app)
        old_graph.graph_snapshot = lambda app_id, version=None: (revision.data, revision.number)
        entries = {str(e.pk): e for e in KnowledgeEntry.objects.filter(application=ws.app)}
        graph_ranks, keyword_ranks, graph_ms, keyword_ms = [], [], [], []
        for repeat in range(5):
            for question in questions:
                start = time.perf_counter()
                cited = old_graph.graph_citations(ws.app.pk, question["question"])
                graph_ms.append((time.perf_counter() - start) * 1000)
                start = time.perf_counter()
                found = old_retrieve(ws.app, question["question"])
                keyword_ms.append((time.perf_counter() - start) * 1000)
                if repeat == 0:
                    graph_ranks.append(
                        ranks(
                            list(dict.fromkeys(c["id"] for c in cited)),
                            question["expected"],
                            entries,
                        )
                    )
                    keyword_ranks.append(
                        ranks(
                            list(dict.fromkeys(str(e.pk) for _, e, _ in found)),
                            question["expected"],
                            entries,
                        )
                    )
        for prefix, found in (("graph", graph_ranks), ("keyword", keyword_ranks)):
            values[f"{prefix}_found"] = sum(1 for r in found if r)
            values[f"{prefix}_top3"] = sum(1 for r in found if r and r <= 3)
            values[f"{prefix}_mrr"] = round(sum(1 / r for r in found if r) / len(found), 3)
        values["graph_p50_ms"] = round(statistics.median(graph_ms), 1)
        values["keyword_p50_ms"] = round(statistics.median(keyword_ms), 1)
        # Citation integrity and agentic evidence only need that commit's ranking.
        cited_all = [
            c for q in questions for c in old_graph.graph_citations(ws.app.pk, q["question"])
        ]
        values["citations_verified"] = round(
            100
            * sum(
                1
                for c in cited_all
                if c["id"] in entries
                and entries[c["id"]].digest == c["digest"]
                and c.get("quote", "") in entries[c["id"]].content
            )
            / len(cited_all),
            1,
        )
        key = json.loads((BENCH / "seeded-evidence.json").read_text(encoding="utf-8"))["tickets"]
        found = expected = self_cited = 0
        for care, identifiers in key.items():
            ticket = ws.tickets[care]
            cited = old_graph.graph_citations(ws.app.pk, ticket_question(ticket))
            text = "\n".join(f"{c['title']}\n{c['excerpt']}" for c in cited)
            found += sum(
                1 for i in identifiers if re.search(rf"(?<![\w-]){re.escape(i)}(?![\w-])", text)
            )
            expected += len(identifiers)
            self_cited += any(c["id"] == str(ticket.pk) for c in cited)
        values["seeded_evidence_recall"] = round(100 * found / expected, 1)
        values["seeded_tickets_self_cited"] = self_cited
        sources = len(revision.data.get("sources", []))
    return {
        "generated_at": timezone.now().isoformat(timespec="seconds"),
        "commit": commit,
        "replayed": True,
        "corpus": {"sources": sources, "questions": len(questions)},
        "metrics": {
            name: {
                "aspect": aspect,
                "label": label,
                "unit": unit,
                "better": better,
                "tolerance": tolerance,
                "value": values.get(name),
                **({} if name in values else {"note": NOT_REPLAYED.get(name, "not replayed")}),
            }
            for name, (aspect, label, unit, better, tolerance) in METRICS.items()
        },
        "details": {},
    }
