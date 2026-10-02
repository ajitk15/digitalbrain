"""Read-only, evidenced links between independently versioned graph snapshots.

These are references, never assertions that an implementation satisfies a rule.
Deriving the projection on demand also works for existing snapshots and avoids
retaining links after a source is withdrawn. No model calls or graph mutations.
"""

import hashlib
import re
from collections import defaultdict

from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views.decorators.http import require_GET

from .models import CodeSnapshot, GraphRevision, KnowledgeEntry
from .workbench import access

IDENTIFIER = re.compile(r"(?<![\w-])[A-Z][A-Z0-9]{0,15}-\d{1,8}(?![\w-])")
PATH = re.compile(
    r"(?<![\w./-])(?:[\w.-]+/)*[\w.-]+\.(?:py|tsx?|jsx?|java|cs|go|rb|rs|php)(?![\w./-])"
)
MAX_FILES = 2000
MAX_LINKS = 100


def identifiers(text):
    return set(IDENTIFIER.findall(text))


def references(revision):
    """Only graph evidence still present in an active, unchanged source."""
    edges = revision.data.get("edges", [])
    source_ids = {e.get("knowledge_id") for e in edges if e.get("knowledge_id")}
    entries = {
        str(e.pk): e for e in KnowledgeEntry.objects.filter(
            application_id=revision.application_id, active=True, pk__in=source_ids
        )
    }
    checked, seen = {}, set()
    source_lines = {key: entry.content.splitlines() for key, entry in entries.items()}
    for edge in edges:
        entry = entries.get(edge.get("knowledge_id"))
        quote = edge.get("evidence", "")
        line = edge.get("line")
        if not entry or not quote or not isinstance(line, int) or line < 1:
            continue
        if entry.pk not in checked:
            checked[entry.pk] = hashlib.sha256(entry.content.encode()).hexdigest() == entry.digest
        lines = source_lines[str(entry.pk)]
        if (not checked[entry.pk] or edge.get("digest") != entry.digest
                or line > len(lines) or quote not in lines[line - 1]):
            continue
        for node in (edge.get("source"), edge.get("target")):
            key = (node, entry.pk, line)
            if node and key not in seen:
                seen.add(key)
                yield node, entry, line, quote


def links(revision, snapshot, *, node_id=None, question=None, entry_id=None, file_id=None):
    if (revision.application_id != snapshot.repository.application_id
            or snapshot.repository.retired_at is not None):
        return [], False
    nodes = {n["id"]: n for n in revision.data.get("nodes", [])}
    query_ids = identifiers(question or "")
    refs = []
    for node, entry, line, quote in references(revision):
        if node_id is not None and node != node_id:
            continue
        if question is not None and not (
            query_ids & identifiers(quote) or str(entry.pk) == str(entry_id)
        ):
            continue
        # Document nodes would attach every paragraph to the same file. Use the
        # precise record/section/value instead; documents can still be selected.
        if node_id is None and nodes.get(node, {}).get("kind") == "document":
            continue
        refs.append((node, entry, line, quote))
    if not refs:
        return [], False
    files, total_bytes, capped = [], 0, False
    for file in snapshot.files.all()[:MAX_FILES + 1].iterator(chunk_size=20):
        total_bytes += len(file.content.encode())
        if len(files) >= MAX_FILES or total_bytes > 8_000_000:
            capped = True
            break
        files.append(file)
    by_path, by_identifier = defaultdict(list), defaultdict(list)
    for file in files:
        if hashlib.sha256(file.content.encode()).hexdigest() != file.digest:
            continue
        parts = file.path.split("/")
        for index in range(len(parts)):
            by_path["/".join(parts[index:])].append(file)
        for line, text in enumerate(file.content.splitlines(), 1):
            for identifier in identifiers(text):
                by_identifier[identifier].append((file, line, text[:1000]))
    result, seen, unique_paths = [], set(), {}
    for node, entry, line, quote in refs:
        candidates = []
        for path in sorted(set(PATH.findall(quote))):
            matches = by_path.get(path, [])
            if path not in unique_paths:
                unique_paths[path] = snapshot.files.filter(
                    Q(path=path) | Q(path__endswith="/" + path)
                ).count() == 1
            # A basename/suffix that names several files proves no unique link.
            if len(matches) == 1 and unique_paths[path]:
                candidates.append((matches[0], 1, "", f"Source references {path}"))
        for identifier in sorted(identifiers(quote)):
            for file, code_line, code_quote in by_identifier.get(identifier, []):
                candidates.append((file, code_line, code_quote, f"Shared identifier {identifier}"))
        for file, code_line, code_quote, reason in candidates:
            if file_id is not None and str(file.pk) != str(file_id):
                continue
            key = (node, entry.pk, line, file.pk, reason)
            if key in seen:
                continue
            seen.add(key)
            result.append({
                "node_id": node, "node_label": nodes.get(node, {}).get("label", "Source"),
                "entry_id": str(entry.pk), "source_title": entry.title,
                "source_digest": entry.digest, "source_line": line, "evidence": quote,
                "file_id": str(file.pk), "path": file.path, "code_digest": file.digest,
                "code_line": code_line, "code_evidence": code_quote, "reason": reason,
                "revision": revision.number, "snapshot": str(snapshot.pk),
                "commit": snapshot.commit_sha, "repository": snapshot.repository.name,
                "url": reverse("code-file", args=[revision.application_id, file.pk]),
                "source_url": reverse("knowledge-detail", args=[revision.application_id, entry.pk]),
                "graph_url": reverse("graph", args=[revision.application_id])
                + f"?version={revision.number}&node={node}",
            })
            if len(result) >= MAX_LINKS:
                return result, True
    return result, capped


@login_required
@require_GET
def related_code(request, pk, number):
    app, _ = access(request.user, pk, "knowledge")
    access(request.user, pk, "code_graph")
    revision = get_object_or_404(GraphRevision, application=app, number=number)
    node = request.GET.get("node", "")
    if node not in {n["id"] for n in revision.data.get("nodes", [])}:
        raise Http404
    found, capped = [], False
    # One current indexed snapshot per active repository; never mix commits.
    repositories = list(app.code_repositories.filter(
        retired_at__isnull=True, status__in=["ready", "partial"]
    ).order_by("id")[:11])
    capped = len(repositories) > 10
    for repository in repositories[:10]:
        snapshot = CodeSnapshot.objects.filter(repository=repository).first()
        if snapshot:
            rows, limited = links(revision, snapshot, node_id=node)
            found.extend(rows)
            capped = capped or limited
    return JsonResponse({"links": found[:MAX_LINKS], "limited": capped or len(found) > MAX_LINKS})
