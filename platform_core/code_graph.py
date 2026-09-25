"""Application-scoped repository graph pages."""

import uuid

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .code_graph_analysis import ROLES
from .code_graph_ingest import refresh_head, register
from .connectors import credential_location
from .models import ApplicationGrant, CodeFile, CodeRepository
from .services import audit
from .workbench import access


class RepositoryForm(forms.Form):
    repository = forms.CharField(
        max_length=240,
        label="GitHub repository",
        widget=forms.TextInput(attrs={"placeholder": "owner/repository"}),
        help_text=(
            "Public repositories work without a credential. Private repositories use "
            "this application's GitHub read credential. The repository is cloned at "
            "its current commit and the checkout is deleted after analysis."
        ),
    )
    ref = forms.CharField(
        max_length=200,
        required=False,
        label="Branch or tag",
        help_text=(
            "Leave blank to use the repository's default branch. The index records "
            "the exact commit."
        ),
    )


#: The chip on a node card. Short enough to sit beside a filename.
LANGUAGE_BADGE = {
    "python": "PY",
    "javascript": "JS",
    "jsx": "JSX",
    "typescript": "TS",
    "tsx": "TSX",
    "java": "JAVA",
    "kotlin": "KT",
    "scala": "SC",
    "go": "GO",
    "rust": "RS",
    "c": "C",
    "cpp": "C++",
    "csharp": "C#",
    "swift": "SW",
    "ruby": "RB",
    "php": "PHP",
}
FUNCTION_KINDS = {"function"}
TYPE_KINDS = {"class", "interface", "enum", "type"}


def count_kind(symbols, kinds):
    """How many of a file's symbols are of these kinds.

    Snapshots taken before symbol kinds were distinguished stored everything as
    "symbol"; those count as neither rather than being guessed at.
    """
    return sum(1 for item in symbols or [] if item.get("kind") in kinds)


#: How far impact is traced, and how many files it will name. Past these the
#: answer stops being "where to look" and becomes "most of the repository".
MAX_HOPS = 6
MAX_REACHED = 200


def reach(snapshot, start_id, upstream):
    """Files reachable from one file, with the number of import hops to each.

    `upstream=False` walks *incoming* edges: who imports this, and who imports
    them - the files a change here could reach. `upstream=True` walks outgoing
    edges: what this rests on.

    Breadth-first, so the hop recorded is the shortest path, and bounded: a
    single well-used module otherwise reaches almost everything, and a list of
    "most of the repository" answers nothing.
    """
    links = {}
    for source, target in snapshot.relationships.values_list("source_id", "target_id"):
        # Upstream follows source -> target (what this rests on); downstream
        # follows target -> source (who would feel a change here).
        start, end = (source, target) if upstream else (target, source)
        links.setdefault(start, []).append(end)

    hops = {start_id: 0}
    frontier = [start_id]
    depth = 0
    while frontier and depth < MAX_HOPS and len(hops) < MAX_REACHED:
        depth += 1
        nxt = []
        for node in frontier:
            for neighbour in links.get(node, ()):
                if neighbour in hops:
                    continue
                hops[neighbour] = depth
                nxt.append(neighbour)
                if len(hops) >= MAX_REACHED:
                    break
            if len(hops) >= MAX_REACHED:
                break
        frontier = nxt
    hops.pop(start_id, None)
    return hops


def rings(snapshot, hops):
    """Reached files grouped by hop distance, nearest first.

    Scoped to the snapshot rather than trusting the ids: `hops` is derived from
    that snapshot's own edges, so the filter costs nothing and is what keeps the
    guarantee local instead of resting on how the caller built the set.
    """
    if not hops:
        return []
    found = {
        item.pk: item
        for item in CodeFile.objects.filter(snapshot=snapshot, pk__in=hops).defer(
            "content", "symbols", "imports"
        )
    }
    grouped = {}
    for file_id, distance in hops.items():
        if file_id in found:
            grouped.setdefault(distance, []).append(found[file_id])
    return [
        {
            "hops": distance,
            "label": "Direct" if distance == 1 else f"{distance} hops away",
            "files": sorted(grouped[distance], key=lambda item: item.path),
        }
        for distance in sorted(grouped)
    ]


def folder_tree(files):
    """Group listed files by folder, deepest path first within each.

    A tree rather than a flat run of 200 rows: the list is how you find a file
    when the canvas is too dense to read, and a flat list of paths is not a way
    of finding anything.
    """
    folders = {}
    for item in files:
        folder = item.path.rsplit("/", 1)[0] if "/" in item.path else ""
        folders.setdefault(folder, []).append(item)
    return [
        {
            "folder": folder or "(repository root)",
            "depth": folder.count("/") if folder else 0,
            "files": folders[folder],
            "parse_issues": sum(1 for item in folders[folder] if not item.parse_ok),
        }
        for folder in sorted(folders)
    ]


#: Languages drawn individually; the rest are summed into one "Other" row.
LANGUAGES_SHOWN = 7


def language_mix(languages):
    """The snapshot's language census as rows for the bar and its legend.

    `x` and `width` are preformatted: they are SVG attributes, and a CSP of
    `style-src 'self'` rules out setting a width any other way.
    """
    rows = [dict(row) for row in languages[:LANGUAGES_SHOWN]]
    rest = languages[LANGUAGES_SHOWN:]
    if rest:
        rows.append(
            {
                "name": f"{len(rest)} other",
                "files": sum(row["files"] for row in rest),
                "share": round(sum(row["share"] for row in rest), 1),
                "analysed": all(row.get("analysed") for row in rest),
                "other": True,
            }
        )
    offset = 0.0
    for index, row in enumerate(rows):
        row["swatch"] = "other" if row.get("other") else index + 1
        row["x"] = f"{offset:.2f}"
        row["width"] = f"{row['share']:.2f}"
        row["label"] = "<0.1%" if row["share"] < 0.1 else f"{row['share']:.1f}%"
        offset += row["share"]
    return rows


def can_manage(grant):
    return grant.role in {ApplicationGrant.Role.OWNER, ApplicationGrant.Role.CONTRIBUTOR}


def selected_repository(app, raw):
    """The repository named in the query string, else the first one.

    An unreadable or unknown id falls back to the first repository rather than
    to nothing: returning None renders the "no repositories yet" empty state
    over an application that plainly has some, which reads as data loss.
    """
    repositories = CodeRepository.objects.filter(application=app, retired_at__isnull=True)
    if raw:
        try:
            chosen = repositories.filter(pk=uuid.UUID(raw)).first()
        except ValueError:
            chosen = None
        if chosen:
            return chosen
    return repositories.first()


@login_required
@require_http_methods(["GET", "POST"])
def repository_add(request, pk):
    """Register a GitHub repository, on a page of its own.

    Opened as a popup from Code Graph by `modal.js`, which fetches this URL and
    lifts its `<main>`; with JavaScript off the button navigates here instead.
    """
    app, grant = access(request.user, pk, "code_graph")
    if not can_manage(grant):
        raise PermissionDenied
    form = RepositoryForm(request.POST or None)
    if request.method == "POST":
        access(request.user, pk, "code_graph", write=True)
        if form.is_valid():
            try:
                repository = register(
                    request.user, pk, form.cleaned_data["repository"], form.cleaned_data["ref"]
                )
            except ValidationError as failure:
                form.add_error("repository", " ".join(failure.messages))
            else:
                messages.success(request, "Repository queued for indexing.")
                return redirect(
                    f"{reverse('code-graph', args=[pk])}?repository={repository.pk}"
                )
    return render(request, "repository_add.html", {"application": app, "form": form})


@login_required
@require_http_methods(["GET", "POST"])
def code_graph(request, pk):
    app, grant = access(request.user, pk, "code_graph")
    form = RepositoryForm(request.POST or None)
    if request.method == "POST":
        access(request.user, pk, "code_graph", write=True)
        if not can_manage(grant):
            raise PermissionDenied
        action = request.POST.get("action")
        if action == "register":
            # The form lives on its own page; a failed submission is shown there.
            return repository_add(request, pk)
        elif action == "refresh":
            repository = get_object_or_404(
                CodeRepository, pk=request.POST.get("repository"), application=app
            )
            repository.status = "queued"
            repository.error = ""
            repository.added_by = request.user
            repository.job_id = uuid.uuid4()
            repository.save(
                update_fields=["status", "error", "added_by", "job_id", "updated_at"]
            )
            messages.success(
                request, "Repository queued for refresh. The last snapshot remains available."
            )
            return redirect(f"{request.path}?repository={repository.pk}")
        elif action == "check":
            # Ask where the branch points, and nothing else. Separate from
            # Refresh index on purpose: this costs one API call and changes
            # nothing, while re-indexing costs a clone and supersedes the
            # snapshot every past run reasoned about. Noticing and acting are
            # two decisions, and only one of them is safe to make for somebody.
            repository = get_object_or_404(
                CodeRepository,
                pk=request.POST.get("repository"),
                application=app,
                retired_at__isnull=True,
            )
            refresh_head(repository)
            repository.refresh_from_db()
            if repository.drifted:
                messages.warning(
                    request,
                    f"{repository.default_ref} has moved on since this snapshot was "
                    f"indexed. Refresh the index to reason about the current code.",
                )
            elif repository.drifted is False:
                messages.success(
                    request, f"Still in sync with {repository.default_ref}."
                )
            else:
                messages.warning(
                    request,
                    f"{repository.name} did not report a head commit for "
                    f"{repository.default_ref}. The last known commit is unchanged.",
                )
            return redirect(f"{request.path}?repository={repository.pk}")
        elif action == "retire":
            repository = get_object_or_404(
                CodeRepository,
                pk=request.POST.get("repository"),
                application=app,
                retired_at__isnull=True,
            )
            repository.retired_at = timezone.now()
            repository.save(update_fields=["retired_at", "updated_at"])
            audit(
                request.user,
                "code_repository.retired",
                repository.pk,
                app.product.portfolio.organization,
                {"repository": repository.name},
            )
            messages.success(
                request,
                f"{repository.name} was removed from this application. Runs no longer "
                "read it, and every past run keeps the snapshot it reasoned about. "
                "Registering the same repository again brings it back.",
            )
            return redirect(request.path)

    repositories = CodeRepository.objects.filter(
        application=app, retired_at__isnull=True
    ).prefetch_related("snapshots")
    repository = selected_repository(app, request.GET.get("repository", ""))
    snapshot = repository.snapshots.first() if repository else None
    search = request.GET.get("q", "").strip()[:200]
    files = CodeFile.objects.none()
    if snapshot:
        files = snapshot.files.all()
        if search:
            files = files.filter(Q(path__icontains=search) | Q(content__icontains=search))
    # One row past the cap, so "there is more" is known without a second
    # count() over the same icontains scan.
    listed_files = list(files.defer("content")[:201])
    more_files = len(listed_files) > 200
    listed_files = listed_files[:200]
    listed_ids = {item.pk for item in listed_files}
    edges = (
        list(
            snapshot.relationships.filter(
                source_id__in=listed_ids, target_id__in=listed_ids
            ).select_related("source", "target")[:600]
        )
        if snapshot
        else []
    )
    graph_data = {
        "nodes": [
            {
                "id": str(item.pk),
                "label": item.path.rsplit("/", 1)[-1],
                "path": item.path,
                "folder": item.path.rsplit("/", 1)[0] if "/" in item.path else "",
                "language": item.language,
                "badge": LANGUAGE_BADGE.get(item.language, item.language[:3].upper()),
                "parse_ok": item.parse_ok,
                # Decided over the whole snapshot at index time. Blank on older
                # snapshots, which the renderer then works out for itself.
                "role": item.role,
                "functions": count_kind(item.symbols, FUNCTION_KINDS),
                "classes": count_kind(item.symbols, TYPE_KINDS),
                "lines": item.lines,
                "url": reverse("code-file", args=[app.pk, item.pk]),
            }
            for item in listed_files
        ],
        "edges": [
            {
                "source": str(edge.source_id),
                "target": str(edge.target_id),
                "confidence": edge.confidence,
                # What crossed the edge. Older snapshots have no names; the
                # renderer simply draws no label for them.
                "kind": edge.kind,
                "label": (
                    ", ".join(
                        (edge.evidence or {}).get("names")
                        or (edge.evidence or {}).get("endpoints")
                        or []
                    )
                )[:60],
            }
            for edge in edges
        ],
    }
    languages = sorted({item.language for item in listed_files if item.language})
    return render(
        request,
        "code_graph.html",
        {
            "application": app,
            "grant": grant,
            "can_manage": can_manage(grant),
            "form": form,
            "repositories": repositories,
            "repository": repository,
            "snapshot": snapshot,
            "files": listed_files,
            "tree": folder_tree(listed_files),
            "edges": edges,
            "graph_data": graph_data,
            "query": search,
            "more_files": more_files,
            "languages": languages,
            "language_mix": language_mix(snapshot.languages) if snapshot else [],
            "roles": ROLES,
            "shown_count": len(listed_files),
            "edge_count": len(edges),
            # The same file the GitHub connector uses. Indexing fails here long
            # before anyone thinks to look at a connector screen, so the answer
            # belongs on this page too.
            "github_credential": credential_location(app, "github"),
            "connector_url": reverse("connectors", args=[app.pk]),
            "file_count": snapshot.files.count() if snapshot else 0,
            "relationship_count": snapshot.relationships.count() if snapshot else 0,
        },
    )


@login_required
@require_http_methods(["GET"])
def code_file(request, pk, file_id):
    app, grant = access(request.user, pk, "code_graph")
    file = get_object_or_404(
        CodeFile.objects.select_related("snapshot__repository"),
        pk=file_id,
        snapshot__repository__application=app,
    )
    dependencies = file.dependencies.filter(snapshot=file.snapshot).select_related("target")
    dependents = file.dependents.filter(snapshot=file.snapshot).select_related("source")
    affected = reach(file.snapshot, file.pk, upstream=False)
    rests_on = reach(file.snapshot, file.pk, upstream=True)
    return render(
        request,
        "code_file.html",
        {
            "application": app,
            "grant": grant,
            "file": file,
            "repository": file.snapshot.repository,
            "snapshot": file.snapshot,
            "dependencies": dependencies,
            "dependents": dependents,
            "functions": count_kind(file.symbols, FUNCTION_KINDS),
            "classes": count_kind(file.symbols, TYPE_KINDS),
            "badge": LANGUAGE_BADGE.get(file.language, file.language[:3].upper()),
            "affected_rings": rings(file.snapshot, affected),
            "affected_count": len(affected),
            "rests_on_rings": rings(file.snapshot, rests_on),
            "rests_on_count": len(rests_on),
            "impact_capped": len(affected) >= MAX_REACHED or len(rests_on) >= MAX_REACHED,
        },
    )
