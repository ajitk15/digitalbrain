"""What a Code Factory run cites besides the knowledge graph: the code and the ticket.

Code passages come from the run's pinned snapshot, ticket passages from the
imported record, and each is labelled with the system it came from. Reads
only, and calls no model. Split out of `code_factory`.
"""

from django.core.exceptions import PermissionDenied
from django.http import Http404

from .models import GraphRevision


def languages_context(languages):
    """What the repository is written in, for a phase deciding what to change.

    Without it the model sees only the files the graph parses, and a Java
    service with one Python script reads as a Python repository - so a design
    confidently proposes Python. When languages the graph does not parse are
    present, the model is told those files exist and are not listed, rather than
    left to conclude they do not. Every name is from `LANGUAGE_NAMES`, a closed
    set, so nothing a repository wrote reaches the prompt here.
    """
    from .code_graph_analysis import describe_languages

    if not languages:
        return []
    lines = [f"LANGUAGES by share of source: {describe_languages(languages)}."]
    unparsed = [row["name"] for row in languages if not row.get("analysed")]
    if unparsed:
        lines.append(
            f"Files in {', '.join(unparsed[:6])} exist in this repository but are not "
            "parsed, so they are not listed below. Propose changes in the language "
            "the affected code is written in."
        )
    return lines


def code_terms(question):
    """The question's terms as ranking reads them: stemmed, no filler, references whole.

    It used to be the first twenty raw words, filler included, so "the" chose
    every file and the snapshot's path order decided which six were cited.
    """
    from .ranking import query_terms

    return query_terms(question)


def rank_files(files, terms):
    """Files holding more of the terms first, then by BM25 over path and text.

    Ties keep order, and a file holding none of them (a linked one) comes last.
    """
    from .ranking import BM25
    from .ranking import terms as split

    index = BM25([split(f"{item.path}\n{item.content}") for item in files])
    ranked = [position for _, position in index.rank_covering(terms)]
    held = set(ranked)
    return [files[position] for position in ranked] + [
        item for position, item in enumerate(files) if position not in held
    ]


def relevant_code_files(run, question):
    """The pinned snapshot's files this question is about, and the terms that chose them.

    Access is re-checked here rather than trusted from triage, because a run
    sits in a queue and a grant or the feature can be withdrawn while it waits.
    A withdrawn one returns nothing; it does not fail the run.
    """
    if not run.code_snapshot_id:
        return [], []
    from django.db.models import Q

    from .workbench import access

    try:
        app, _ = access(run.requested_by, run.application_id, "code_graph")
    except (Http404, PermissionDenied):
        return [], []
    if run.code_snapshot.repository.application_id != app.pk:
        return [], []
    if run.code_snapshot.repository.retired_at is not None:
        return [], []
    terms = code_terms(question)
    condition = Q()
    for term in terms:
        condition |= Q(path__icontains=term) | Q(content__icontains=term)
    candidates = list(run.code_snapshot.files.filter(condition)) if terms else []
    for file, link in linked_files(run, app, question):
        file.knowledge_link = link
        candidates = [f for f in candidates if f.pk != file.pk] + [file]
    # Every candidate - a file holding a term, or one a requirement it shares
    # with the question links to - is ranked by how much of the question it
    # holds, never by where its path sorts. Linked files used to come first in
    # the order their sources were read, and on CarePath they took every place:
    # CARE-412's unbounded search is the one route that does not name NFR-06,
    # so the file with the bug was never shown (3 of 7 planted-bug files cited
    # before, 5 of 7 after; `manage.py scorecard`).
    files = rank_files(candidates, terms)[:20]
    if not files:
        files = list(run.code_snapshot.files.defer("content")[:12])
    return files, terms


def linked_files(run, app, question):
    """(file, link) for each snapshot file the published graph links to this question."""
    from .code_knowledge import links
    from .models import CodeFile
    from .workbench import access

    try:
        access(run.requested_by, run.application_id, "knowledge")
    except (Http404, PermissionDenied):
        return []
    revision = GraphRevision.objects.filter(
        application=app, number=run.graph_version, published_at__isnull=False
    ).first()
    if not revision:
        return []
    linked, _ = links(revision, run.code_snapshot, question=question)
    reasons = {}
    for link in linked:
        reasons.setdefault(link["file_id"], link)
    files = {str(f.pk): f for f in CodeFile.objects.filter(
        snapshot=run.code_snapshot, pk__in=reasons
    )}
    return [(files[key], link) for key, link in reasons.items()]


#: How many files of the pinned snapshot are offered as citable evidence, and
#: how much of each. The structural summary still covers up to twenty.
CODE_EVIDENCE_FILES = 6
CODE_EXCERPT_LINES = 14


def code_excerpt(content, terms):
    """A window of the file centred on the line holding most of the terms, exactly as stored.

    The first line holding any term used to anchor it, which was usually an
    import or the module's docstring rather than the code the ticket is about.
    Cut from the file's own lines with their endings kept, so the excerpt is a
    substring of the stored content and can be re-checked like any quotation.
    """
    from .ranking import terms as split

    lines = content.splitlines(keepends=True)
    if not lines:
        return "", 0
    wanted = set(terms)
    held = [len(wanted.intersection(split(line))) for line in lines]
    best = max(range(len(lines)), key=lambda index: (held[index], -index))
    start = max(0, min(best - CODE_EXCERPT_LINES // 2, len(lines) - CODE_EXCERPT_LINES))
    excerpt = "".join(lines[start : start + CODE_EXCERPT_LINES]).rstrip()
    return excerpt[:1500], start + 1


def code_citations(run, question):
    """Citable passages from the pinned code snapshot, labelled as Code Graph.

    The structural summary above tells the model what exists; these are what
    an item may *cite*, so a gap like "the export path never checks consent"
    can point at the file that shows it. Each carries the file's digest, and
    approval re-checks it against the snapshot just as it re-checks knowledge.
    """
    from .models import CodeFile

    files, terms = relevant_code_files(run, question)
    if not files:
        return []
    snapshot = run.code_snapshot
    label = f"Code graph · {snapshot.repository.name} v{snapshot.number}"
    found = []
    content_by_id = {f.pk: f for f in CodeFile.objects.filter(
        pk__in=[f.pk for f in files[:CODE_EVIDENCE_FILES]]
    )}
    for selected in files[:CODE_EVIDENCE_FILES]:
        item = content_by_id[selected.pk]
        excerpt, line = code_excerpt(item.content, terms)
        link = getattr(selected, "knowledge_link", None)
        if link and link["code_evidence"]:
            excerpt, line = code_excerpt(item.content, code_terms(link["code_evidence"]))
        if not excerpt or excerpt not in item.content:
            continue
        found.append(
            {
                "id": f"code:{item.pk}",
                "origin": "code_graph",
                "origin_label": label,
                "title": f"{item.path} · line {line}",
                "excerpt": excerpt,
                "digest": item.digest,
                "snapshot": str(snapshot.pk),
                "knowledge_link": link,
            }
        )
    return found


def source_system(entry):
    """Which system a knowledge entry came from, read from its own record."""
    source = (entry.source or "").casefold()
    if "nav_to.do?uri=" in source:
        return "ServiceNow"
    if "github.com/" in source and "/issues/" in source:
        return "GitHub"
    if "/browse/" in source:
        return "Jira"
    if entry.document_id:
        return "Document"
    return "Knowledge"


def labelled_graph_citations(citations, version):
    """Graph citations, each saying it came through the knowledge graph - and from
    which system the passage itself was imported."""
    from .models import KnowledgeEntry

    entries = {
        str(entry.pk): entry
        for entry in KnowledgeEntry.objects.filter(
            pk__in=[citation["id"] for citation in citations]
        ).only("id", "source", "document_id")
    }
    return [
        {
            **citation,
            "origin": "knowledge_graph",
            "origin_label": f"Knowledge graph v{citation.get('graph_version') or version}",
            "system": source_system(entries[citation["id"]])
            if citation["id"] in entries
            else "Knowledge",
        }
        for citation in citations
    ]


def ticket_citation(run):
    """The ticket itself, as evidence an item may cite: "the ticket says so".

    Only an imported ticket - a hand-written one has no source record to check
    a quotation against, so it stays the question and is never evidence.
    """
    from .models import KnowledgeEntry
    from .serviceops import fields_and_description

    if not run.ticket_url or run.ticket_body:
        return []
    entry = KnowledgeEntry.objects.filter(
        application_id=run.application_id, source=run.ticket_url
    ).answerable().first()
    from .permissions import permitted

    if entry is None or not permitted(entry, run.requested_by):
        return []
    _, description = fields_and_description(entry)
    excerpt = (description or entry.content)[:1500].rstrip()
    if not excerpt or excerpt not in entry.content:
        return []
    system = source_system(entry)
    return [
        {
            "id": str(entry.pk),
            "origin": "ticket",
            "origin_label": f"{system} {run.ticket_external_id}".strip(),
            "system": system,
            "title": run.ticket_title or entry.title,
            "excerpt": excerpt,
            "digest": entry.digest,
        }
    ]


def ticket_entry(run):
    """The imported ticket a run is about, as it stands now - or None.

    A hand-written ticket has no record behind it, so there is nothing to
    point into.
    """
    from .models import KnowledgeEntry

    if not run or not run.ticket_url or run.ticket_body:
        return None
    return KnowledgeEntry.objects.filter(
        application_id=run.application_id, source=run.ticket_url
    ).answerable().first()


def ticket_label(run):
    """How the ticket is named on a tag: "Jira KAN-4", "GitHub #12"."""
    from .models import KnowledgeEntry

    if not run:
        return ""
    if not run.ticket_url:
        return "Ticket (typed in)" if run.ticket_body else ""
    system = source_system(KnowledgeEntry(source=run.ticket_url))
    if system in {"Knowledge", "Document"}:
        system = "Ticket"
    return f"{system} {run.ticket_external_id}".strip()


#: Words too common to say two sentences are about the same thing.
TICKET_STOP_WORDS = frozenset(
    "the and for with that this must should are was were not when from into have has will "
    "been its their there which what where than then also only each such does did can may "
    "any all but our your they them you who whose one ones how why use used being "
    "ticket asks explicitly requires required".split()
)
WORD = r"[A-Za-z][A-Za-z0-9_]*"


def ticket_passages(run, item, limit=3):
    """The sentences of the ticket that state this gap, with the shared words marked.

    Found by plain word overlap between the gap and each sentence of the ticket,
    not by a model - so it can always say why a sentence was chosen. Each one is
    cut from the ticket as it stands now and kept only while it still appears
    there word for word.
    """
    import re

    from .serviceops import fields_and_description, stem

    entry = ticket_entry(run)
    if entry is None:
        return []
    _, description = fields_and_description(entry)
    # A ticket with no header lines has no separate body: search all of it.
    description = description or entry.content

    def words(text):
        # Stemmed, so "overflow" and "overflows" are the same word.
        found = {word.lower() for word in re.findall(WORD, text)}
        return {stem(word) for word in found if len(word) >= 3 and word not in TICKET_STOP_WORDS}

    wanted = words(f"{item.title} {item.explanation}")
    scored = []
    # A sentence ends at . ; ! ? followed by a space or the end - not inside
    # "consent.current()" or a version number.
    for match in re.finditer(r"(?:[^.;!?\n]|[.;!?](?=\S))+[.;!?]?", description):
        text = match.group().strip()
        if len(text) < 12:
            continue
        shared = wanted & words(text)
        if len(shared) >= 2:
            scored.append((len(shared), match.start(), text[:600], shared))
    # A sentence sharing a couple of common words with the gap is noise next to
    # one that restates it: keep only those within half of the best match.
    top = max((row[0] for row in scored), default=0)
    scored = [row for row in scored if row[0] * 2 >= top]
    best = sorted(scored, key=lambda row: (-row[0], row[1]))[:limit]
    passages = []
    for _, _, text, shared in sorted(best, key=lambda row: row[1]):
        if text not in entry.content:
            continue
        parts = [
            {"text": part, "hit": stem(part.lower()) in shared}
            for part in re.split(f"({WORD})", text)
            if part
        ]
        passages.append({"text": text, "parts": parts, "shared": sorted(shared)})
    return passages


#: How each origin is named on the page, in the order it is listed.
ORIGINS = (
    ("ticket", "Ticket"),
    ("knowledge_graph", "Knowledge"),
    ("code_graph", "Code"),
)

#: What a stored label used to start with, for each origin it names.
STORED_PREFIXES = {"knowledge_graph": "Knowledge graph", "code_graph": "Code graph"}


def origin_name(citation):
    """A citation's origin as people read it: "Knowledge v7", "Code · acme/app v2".

    Worked out when the page is drawn, from the origin and the version, rather
    than shown as `origin_label`: that label was written into every plan when
    its run was analysed, and is what the model reads in each evidence title,
    so renaming it at the source would leave old runs in the old words and
    change the prompt. A code label's repository and snapshot are taken from
    what follows its stored prefix; a ticket's label is its system and key.
    """
    origin = citation.get("origin") or ("knowledge_graph" if citation.get("graph_version") else "")
    names = dict(ORIGINS)
    if origin == "knowledge_graph":
        version = citation.get("graph_version")
        return f"{names[origin]} v{version}" if version else names[origin]
    stored = citation.get("origin_label") or ""
    prefix = STORED_PREFIXES.get(origin)
    if prefix:
        return names[origin] + stored.removeprefix(prefix)
    return stored or names.get(origin, "")


def code_context_for(run, question):
    """A bounded structural neighborhood from the snapshot pinned for this run.

    Access is re-checked here rather than trusted from triage, because a run
    sits in a queue and a grant or the feature can be withdrawn while it waits.
    A withdrawn one removes the code context; it does not fail the run. Code
    Graph is additional evidence for a pipeline that worked without it, and
    turning the feature off must not take Code Factory down with it.
    """
    files, _ = relevant_code_files(run, question)
    if not files:
        return ""
    from django.db.models import Q

    from .models import CodeRelationship

    ids = {item.pk for item in files}
    edges = (
        CodeRelationship.objects.filter(snapshot=run.code_snapshot)
        .filter(Q(source_id__in=ids) | Q(target_id__in=ids))
        .select_related("source", "target")[:40]
    )
    lines = [
        f"Code Graph: {run.code_snapshot.repository.name} snapshot v{run.code_snapshot.number} "
        f"at commit {run.code_snapshot.commit_sha}."
    ]
    lines += languages_context(run.code_snapshot.languages)
    for item in files:
        link = getattr(item, "knowledge_link", None)
        if link:
            lines.append(
                f"REFERENCE {item.path}: {link['reason']}; knowledge v{link['revision']}, "
                f"{link['source_title']} line {link['source_line']}. "
                "A reference is not proof of implementation correctness."
            )
        symbols = ", ".join(symbol.get("name", "") for symbol in item.symbols[:12])
        description = symbols or "no detected symbols"
        lines.append(
            f"FILE {item.path} ({item.language}, {item.lines} lines): {description}"
        )
    for edge in edges:
        label = edge.get_confidence_display()
        lines.append(f"DEPENDENCY [{label}] {edge.source.path} imports {edge.target.path}")
    return "\n".join(lines)[:12000]
