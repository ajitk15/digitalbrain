"""One inventory for uploaded documents and searchable imported records."""

from urllib.parse import urlparse

from django.core.paginator import Paginator
from django.db.models import CharField, Count, Prefetch, Q, Value

from .models import CodeRepository, Document, GraphRevision, KnowledgeEntry, KnowledgeSource
from .services import feature_enabled


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


def graph_usage(app, entries):
    """Match snapshot evidence to the exact source revision, never just its title."""
    usage = {(str(entry.pk), entry.digest): [] for entry in entries}
    if not usage:
        return {}
    revisions = GraphRevision.objects.filter(application=app).values_list(
        "number", "published_at", "data__sources"
    )
    for number, published_at, sources in revisions.iterator():
        matched = set()
        for source in sources or []:
            key = (source.get("id"), source.get("digest"))
            if key in usage and key not in matched:
                usage[key].append({"number": number, "published": bool(published_at)})
                matched.add(key)
    return {source_id: versions for (source_id, digest), versions in usage.items()}


def library_context(app, request):
    enabled = feature_enabled("knowledge", app)
    documents = Document.objects.filter(application=app).exclude(status="deleted")
    entries = KnowledgeEntry.objects.filter(application=app, active=True)
    standalone = entries.filter(document__isnull=True)
    search = request.GET.get("q", "").strip()[:200]
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
    inventory = documents.order_by().annotate(
        kind=Value("document", output_field=CharField())
    ).values_list("pk", "created_at", "kind")
    if enabled:
        inventory = inventory.union(standalone.order_by().annotate(
            kind=Value("entry", output_field=CharField())
        ).values_list("pk", "created_at", "kind"))
    page = Paginator(inventory.order_by("-created_at", "-pk"), 20).get_page(
        request.GET.get("page")
    )
    selected = list(page.object_list)
    docs = {doc.pk: doc for doc in documents.filter(
        pk__in=[pk for pk, _, kind in selected if kind == "document"]
    )}
    page_entries = list(entries.filter(
        Q(document_id__in=docs) | Q(pk__in=[pk for pk, _, kind in selected if kind == "entry"])
    ).defer("content")) if enabled else []
    by_document = {entry.document_id: entry for entry in page_entries if entry.document_id}
    by_id = {entry.pk: entry for entry in page_entries}
    usage = graph_usage(app, page_entries)
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
        return {
            "document": doc, "entry": entry,
            "title": doc.name if doc else entry.title,
            "origin": doc.get_origin_display() if doc else (
                "Imported record" if entry.source else "Knowledge record"
            ),
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
        usage.update(graph_usage(app, grouped_entries))
        for origin in origins:
            origin.rows = [
                build_row(doc, grouped_by_document.get(doc.pk), doc.created_at)
                for doc in origin.files
            ]
    return {
        "page": page, "query": search, "source_count": count,
        # The grouped view answers "what came from where"; the table answers
        # "find me this file". Only one of them is the right shape at a time.
        "grouped": not search,
        # The places documents came from, so the list can be read as "these
        # twenty-seven files, from here" rather than as twenty-seven unrelated
        # rows that happen to share a prefix.
        "origins": origins,
        "knowledge_enabled": enabled,
        "conversion_pending": documents.filter(status__in=Document.IN_FLIGHT).exists(),
    }
