"""Application-scoped repository graph pages."""

import uuid

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from .code_graph_ingest import register
from .models import ApplicationGrant, CodeFile, CodeRepository
from .workbench import access


class RepositoryForm(forms.Form):
    repository = forms.CharField(
        max_length=240,
        label="GitHub repository",
        widget=forms.TextInput(attrs={"placeholder": "owner/repository"}),
        help_text=(
            "Public repositories work without a credential. Private repositories use "
            "this application's GitHub read credential."
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


def can_manage(grant):
    return grant.role in {ApplicationGrant.Role.OWNER, ApplicationGrant.Role.CONTRIBUTOR}


def selected_repository(app, raw):
    """The repository named in the query string, else the first one.

    An unreadable or unknown id falls back to the first repository rather than
    to nothing: returning None renders the "no repositories yet" empty state
    over an application that plainly has some, which reads as data loss.
    """
    repositories = CodeRepository.objects.filter(application=app)
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
def code_graph(request, pk):
    app, grant = access(request.user, pk, "code_graph")
    form = RepositoryForm(request.POST or None)
    if request.method == "POST":
        access(request.user, pk, "code_graph", write=True)
        if not can_manage(grant):
            raise PermissionDenied
        action = request.POST.get("action")
        if action == "register" and form.is_valid():
            try:
                repository = register(
                    request.user, pk, form.cleaned_data["repository"], form.cleaned_data["ref"]
                )
            except ValidationError as failure:
                form.add_error("repository", " ".join(failure.messages))
            else:
                messages.success(request, "Repository queued for indexing.")
                return redirect(f"{request.path}?repository={repository.pk}")
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

    repositories = CodeRepository.objects.filter(application=app).prefetch_related("snapshots")
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
                "language": item.language,
                "parse_ok": item.parse_ok,
                "url": reverse("code-file", args=[app.pk, item.pk]),
            }
            for item in listed_files
        ],
        "edges": [
            {
                "source": str(edge.source_id),
                "target": str(edge.target_id),
                "confidence": edge.confidence,
            }
            for edge in edges
        ],
    }
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
            "edges": edges,
            "graph_data": graph_data,
            "query": search,
            "more_files": more_files,
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
        },
    )
