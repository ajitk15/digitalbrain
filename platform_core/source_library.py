"""One inventory for uploaded documents and searchable imported records."""

from pathlib import PurePosixPath
from urllib.parse import urlencode, urlparse

from django.core.paginator import Paginator
from django.db.models import CharField, Count, Prefetch, Q, Value
from django.db.models.functions import Lower

from . import sensitivity, sorting, source_map
from .models import (
    CodeRepository,
    Connector,
    Document,
    GraphRevision,
    KnowledgeEntry,
    KnowledgeSource,
)
from .services import feature_enabled
from .source_quality import for_entries
from .ticket_quality import assess

#: Connector kinds whose records are tickets someone will work from, and so
#: worth scoring for readiness. ServiceNow incidents and automations are not.
SCORED_KINDS = {"jira"}

#: What a file is, by its extension, in the words someone who uploaded it uses.
#: Anything else is shown as its extension.
FILE_TYPES = {
    ".pdf": "PDF",
    ".doc": "Word", ".docx": "Word",
    ".xls": "Excel", ".xlsx": "Excel",
    ".ppt": "PowerPoint", ".pptx": "PowerPoint",
    ".csv": "CSV",
    ".md": "Markdown", ".markdown": "Markdown",
    ".htm": "HTML", ".html": "HTML",
    ".txt": "Text",
    ".json": "JSON", ".xml": "XML",
    ".rtf": "Rich text", ".epub": "EPUB",
    ".png": "Image", ".jpg": "Image", ".jpeg": "Image", ".tif": "Image", ".tiff": "Image",
}


def file_type(name):
    """("PDF", "pdf") for "folder/report.PDF": a label and the bare extension."""
    suffix = PurePosixPath(name.replace("\\", "/")).suffix.lower()
    return FILE_TYPES.get(suffix, suffix[1:].upper() or "File"), suffix[1:]


def personal(entry):
    """The PHI / PII tag for a row: which, and what was found, in words."""
    found = entry.personal_data if entry else None
    masked = entry.masked if entry else None
    if not found and not masked:
        return None
    return {
        "category": sensitivity.category(found),
        "summary": sensitivity.summary(found),
        "masked": sensitivity.summary(masked or {}),
    }


def repository_of(url):
    """owner/name for a GitHub document URL, read from the path, not a substring.

    A substring search matches the wrong repository whenever one repository's
    owner/name also appears inside another's file path, and misses a bare
    ``github.com/owner/repo`` link because it has no trailing slash.
    """
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    if host not in {"github.com", "www.github.com", "raw.githubusercontent.com"}:
        return ""
    parts = [part for part in (parsed.path or "").split("/") if part]
    return f"{parts[0]}/{parts[1]}".lower() if len(parts) >= 2 else ""


def connector_index(app):
    """Every connector's record prefix, longest first, for `connector_of`.

    KnowledgeEntry deliberately has no connector - knowledge outlives the
    connector that brought it in - so the source URL is matched against each
    connector's namespace, the same test `connectors.prune` uses to decide
    which knowledge is a connector's own.
    """
    from .code_factory import connector_prefix
    from .connector_kinds import KINDS

    by_prefix = {}
    for connector in Connector.objects.filter(application=app):
        prefix = connector_prefix(connector)
        if prefix and connector.kind in KINDS:
            by_prefix.setdefault(prefix, []).append(connector)
    return sorted(
        ((prefix, connectors) for prefix, connectors in by_prefix.items()),
        key=lambda item: len(item[0]),
        reverse=True,
    )


def connector_of(source, index):
    """(kind, connector or None) for the connector a source URL came from.

    The connector is None when two connectors share the namespace - two
    filters onto one Jira - because naming either would be a guess. A source
    no connector claims, or whose connector has been removed, is (None, None).
    """
    from .connector_kinds import KINDS

    for prefix, connectors in index:
        if source and source.startswith(prefix):
            return KINDS[connectors[0].kind], (connectors[0] if len(connectors) == 1 else None)
    return None, None


def imported_label(kind, connector):
    """"Imported from Jira", plus the connector's name when it says more."""
    if kind is None:
        return "Imported record"
    label = f"Imported from {kind.label}"
    if connector and connector.name and connector.name.strip().lower() != kind.label.lower():
        label += f" · {connector.name}"
    return label


def entry_origin(app, entry, index=None):
    """What the Sources table and the record page say about a standalone entry."""
    if not entry.source:
        return {"label": "Knowledge record", "kind": None, "connector": None}
    kind, connector = connector_of(entry.source, connector_index(app) if index is None else index)
    return {"label": imported_label(kind, connector), "kind": kind, "connector": connector}


def ticket_quality(entry, origin, content=None):
    """The readiness score for an imported ticket, or None for anything else."""
    if origin["kind"] is None or origin["kind"].key not in SCORED_KINDS:
        return None
    return assess(entry.title, entry.content if content is None else content)


def graph_usage(app, entries):
    """Match snapshot evidence to the exact source revision, never just its title."""
    usage = {(str(entry.pk), entry.digest): [] for entry in entries}
    if not usage:
        return {}
    from .graph_store import read

    revisions = GraphRevision.objects.filter(application=app).values_list(
        "number", "published_at", "data__sources", "data__slimmed"
    )
    for number, published_at, sources, slimmed in revisions.iterator():
        if slimmed:
            # Its sources are rows only now (graph_store.slim).
            version = GraphRevision(application_id=app.pk, number=number, stored=True)
            sources = read(version, "sources")
        matched = set()
        for source in sources or []:
            key = (source.get("id"), source.get("digest"))
            if key in usage and key not in matched:
                usage[key].append({"number": number, "published": bool(published_at)})
                matched.add(key)
    return {source_id: versions for (source_id, digest), versions in usage.items()}


#: The Documents table sorts by name or by when a source was added (newest
#: first). The other columns are worked out per row after the page is read.
LIBRARY_COLUMNS = {
    "source": sorting.Column("Source", field="label"),
    "added": sorting.Column("Added (UTC)", "desc", field="created_at"),
}


def library_context(app, request):
    from .ticket_library import ticket_filter

    enabled = feature_enabled("knowledge", app)
    documents = Document.objects.filter(application=app).exclude(status="deleted")
    entries = KnowledgeEntry.objects.filter(application=app, active=True)
    standalone = entries.filter(document__isnull=True)
    # Tickets are listed on their own tab (`ticket_library`); a record whose
    # connector is gone is claimed by none and stays here as "Imported record".
    tickets = ticket_filter(app)
    if tickets is not None:
        standalone = standalone.exclude(tickets)
    search = request.GET.get("q", "").strip()[:200]
    # A search here still says when tickets match, so nothing that used to be
    # found on this page disappears without a word.
    ticket_matches = (
        entries.filter(document__isnull=True).filter(tickets).filter(
            Q(title__icontains=search) | Q(content__icontains=search)
        ).count()
        if search and enabled and tickets is not None
        else 0
    )
    count = documents.count() + (standalone.count() if enabled else 0)
    if search:
        condition = Q(name__icontains=search) | Q(source_url__icontains=search)
        if enabled:
            condition |= Q(
                knowledgeentry__application=app, knowledgeentry__active=True
            ) & (Q(knowledgeentry__title__icontains=search)
                 | Q(knowledgeentry__content__icontains=search))
        documents = documents.filter(condition)
        standalone = standalone.filter(
            Q(title__icontains=search) | Q(content__icontains=search)
            | Q(source__icontains=search)
        )
    else:
        # Without a search, a document that came from a source is listed under
        # that source and nowhere else. Listing it here as well put every
        # imported file on the page twice - once under the origin it belongs to
        # and once in a table it shared with everything else.
        #
        # Searching is the exception, and deliberately so: someone looking for a
        # file by name wants it found wherever it lives, and grouping the answer
        # by origin would hide matches inside sources they have not opened.
        documents = documents.filter(source__isnull=True)
    # A file is named by its file name and a record by its title, so the union
    # carries one lower-cased label for the Source heading to sort on.
    inventory = documents.order_by().annotate(
        kind=Value("document", output_field=CharField()), label=Lower("name"),
    ).values_list("pk", "created_at", "kind", "label")
    if enabled:
        inventory = inventory.union(standalone.order_by().annotate(
            kind=Value("entry", output_field=CharField()), label=Lower("title"),
        ).values_list("pk", "created_at", "kind", "label"))
    sort, direction = sorting.choose(request, LIBRARY_COLUMNS, "added")
    # A union orders by its own column names, not by expressions.
    field = LIBRARY_COLUMNS[sort].field
    page = Paginator(
        inventory.order_by(f"-{field}" if direction == "desc" else field, "-pk"), 20
    ).get_page(request.GET.get("page"))
    selected = [(pk, created_at, kind) for pk, created_at, kind, _ in page.object_list]
    docs = {doc.pk: doc for doc in documents.filter(
        pk__in=[pk for pk, _, kind in selected if kind == "document"]
    )}
    page_entries = list(entries.filter(
        Q(document_id__in=docs) | Q(pk__in=[pk for pk, _, kind in selected if kind == "entry"])
    ).defer("content")) if enabled else []
    by_document = {entry.document_id: entry for entry in page_entries if entry.document_id}
    by_id = {entry.pk: entry for entry in page_entries}
    usage = graph_usage(app, page_entries)
    # Read only when this page lists an imported record, so a library of
    # uploaded files pays no query for connectors it does not show.
    connectors = (
        connector_index(app)
        if any(entry.source and not entry.document_id for entry in page_entries)
        else []
    )
    origins_of = {
        entry.pk: entry_origin(app, entry, connectors)
        for entry in page_entries
        if not entry.document_id
    }
    # Documents get the source check, read from what was stored when they were
    # converted rather than from their deferred text.
    checks = for_entries([entry for entry in page_entries if entry.document_id])
    code_repositories = (
        {
            repository.external_id.lower(): repository
            for repository in CodeRepository.objects.filter(application=app).prefetch_related(
                "snapshots"
            )
        }
        if feature_enabled("code_graph", app)
        else {}
    )
    def build_row(doc, entry, created_at):
        """One table row, whether it is shown in the table or under its origin.

        Both views carry the same columns, so both are built here. Writing the
        row twice is how the two quietly come to disagree about what a source
        looks like.
        """
        code_repository = (
            code_repositories.get(repository_of(doc.source_url))
            if doc and doc.origin == "github"
            else None
        )
        origin = origins_of.get(entry.pk) if entry and not doc else None
        return {
            "document": doc, "entry": entry,
            "title": doc.name if doc else entry.title,
            "origin": doc.get_origin_display() if doc else (
                origin["label"] if origin else "Knowledge record"
            ),
            "check": checks.get(entry.pk) if entry else None,
            "file_type": file_type(doc.name) if doc else None,
            "personal": personal(entry),
            "created_at": created_at,
            "versions": usage.get(str(entry.pk), []) if entry else [],
            "code_repository": code_repository,
            "code_snapshot": code_repository.snapshots.first() if code_repository else None,
        }

    rows = []
    for pk, created_at, kind in selected:
        doc = docs.get(pk) if kind == "document" else None
        entry = by_document.get(pk) if doc else by_id.get(pk)
        rows.append(build_row(doc, entry, created_at))
    page.object_list = rows
    # Only a page that holds a checked document gets the column, so a page of
    # imported records is not given a column of dashes. Tickets are scored on
    # their own tab.
    quality_column = any(row["check"] for row in rows)

    origins = list(
        KnowledgeSource.objects.filter(application=app)
        .prefetch_related(
            # One query for every source's files, however many sources there
            # are. Reading them per source would be a query per row, which is
            # the shape this page spent a release getting rid of.
            Prefetch(
                "documents",
                queryset=Document.objects.exclude(status="deleted").order_by("name"),
                to_attr="files",
            )
        )
        .annotate(
            file_count=Count(
                "documents", filter=~Q(documents__status="deleted"), distinct=True
            ),
            orphan_count=Count(
                "documents",
                filter=Q(documents__orphaned=True) & ~Q(documents__status="deleted"),
                distinct=True,
            ),
            # Counted here because the origin row reads as a summary of the
            # import, and a file count alone lets "1 file" stand for a file that
            # arrived with nothing in it. The status beside it answers a
            # different question - whether the origin has moved - and must keep
            # meaning only that, so the outcome of the conversion is said
            # separately rather than folded into it.
            failed_count=Count(
                "documents",
                filter=Q(documents__status="failed"),
                distinct=True,
            ),
        )
        .order_by("name")
    )
    if origins:
        # The rows under each origin need what the table's rows need: the
        # converted entry, which graph versions used it, and the repository it
        # belongs to. Gathered for every origin at once rather than per origin,
        # so the cost does not grow with the number of sources.
        grouped_documents = [doc for origin in origins for doc in origin.files]
        grouped_entries = (
            list(entries.filter(document_id__in=[d.pk for d in grouped_documents]).defer("content"))
            if enabled and grouped_documents
            else []
        )
        grouped_by_document = {entry.document_id: entry for entry in grouped_entries}
        checks.update(for_entries(grouped_entries))
        usage.update(graph_usage(app, grouped_entries))
        for origin in origins:
            origin.rows = [
                build_row(doc, grouped_by_document.get(doc.pk), doc.created_at)
                for doc in origin.files
            ]
    return {
        "page": page, "query": search, "source_count": count,
        "columns": sorting.headers(request, LIBRARY_COLUMNS, sort, direction),
        "extra_query": urlencode(sorting.carried(sort, direction, LIBRARY_COLUMNS, "added")),
        "ticket_matches": ticket_matches,
        # The grouped view answers "what came from where"; the table answers
        # "find me this file". Only one of them is the right shape at a time.
        "grouped": not search,
        # The places documents came from, so the list can be read as "these
        # twenty-seven files, from here" rather than as twenty-seven unrelated
        # rows that happen to share a prefix.
        "origins": origins,
        "knowledge_enabled": enabled,
        "quality_column": quality_column,
        # How the set reads and connects, in one line above the table. Worked
        # out once per graph fingerprint, so a second visit reads no text.
        "source_summary": source_map.summary(app.pk) if enabled and count else None,
        "conversion_pending": documents.filter(status__in=Document.IN_FLIGHT).exists(),
    }
