"""One inventory for uploaded documents and searchable imported records."""

from django.core.paginator import Paginator
from django.db.models import CharField, Q, Value

from .models import Document, GraphRevision, KnowledgeEntry
from .services import feature_enabled


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
    rows = []
    for pk, created_at, kind in selected:
        doc = docs.get(pk) if kind == "document" else None
        entry = by_document.get(pk) if doc else by_id.get(pk)
        rows.append({
            "document": doc, "entry": entry,
            "title": doc.name if doc else entry.title,
            "origin": doc.get_origin_display() if doc else (
                "Imported record" if entry.source else "Knowledge record"
            ),
            "created_at": created_at,
            "versions": usage.get(str(entry.pk), []) if entry else [],
        })
    page.object_list = rows
    return {
        "page": page, "query": search, "source_count": count,
        "knowledge_enabled": enabled,
        "conversion_pending": documents.filter(status__in=[
            "pending", "fetching", "queued", "converting"
        ]).exists(),
    }
