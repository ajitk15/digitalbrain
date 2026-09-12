"""Private application-scoped document intake. Originals are preserved privately."""

import hashlib
import json
import os
import uuid
from pathlib import Path

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from .models import ApplicationGrant, Document, KnowledgeEntry
from .policy import application_for
from .services import audit, feature_enabled

MAX_BYTES = 20 * 1024 * 1024


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    widget = MultipleFileInput

    def clean(self, data, initial=None):
        values = data if isinstance(data, (list, tuple)) else [data]
        if len(values) > 20:
            raise forms.ValidationError("Upload up to 20 documents at a time.")
        uploads = [super(MultipleFileField, self).clean(value, initial) for value in values]
        if sum(upload.size for upload in uploads) > MAX_BYTES:
            raise forms.ValidationError("Keep the combined upload size within 20 MB.")
        return uploads


class DocumentForm(forms.Form):
    file = MultipleFileField(
        label="Choose documents",
        help_text="All file types. Select up to 20 files, 20 MB total per upload.",
    )


def intake_enabled():
    # Production requires application-specific private storage and a scanner before enabling.
    return not settings.PRODUCTION


def store_document(actor, application_id, upload):
    app, grant = application_for(actor, application_id)
    if grant.role not in {ApplicationGrant.Role.OWNER, ApplicationGrant.Role.CONTRIBUTOR}:
        raise PermissionDenied
    if not intake_enabled() or not feature_enabled("document_uploads", app):
        raise PermissionDenied("Production document intake requires private storage integration.")
    document_id = uuid.uuid4()
    root = Path(settings.BASE_DIR) / ".runtime" / "documents"
    folder = root / str(app.organization_id) / str(app.pk)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = folder / f"{document_id}.quarantine"
    digest = hashlib.sha256()
    size = 0
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as destination:
            for chunk in upload.chunks():
                size += len(chunk)
                if size > MAX_BYTES:
                    raise forms.ValidationError("Document exceeds 20 MB.")
                digest.update(chunk)
                destination.write(chunk)
        with transaction.atomic():
            # Recheck authorization after receiving the file.
            app, current_grant = application_for(actor, application_id)
            if current_grant.role not in {"owner", "contributor"} or not feature_enabled(
                "document_uploads", app
            ):
                raise PermissionDenied
            doc = Document.objects.create(
                id=document_id,
                application=app,
                uploaded_by=actor,
                name=Path(upload.name.replace("\\", "/")).name[:200],
                size=size,
                sha256=digest.hexdigest(),
                status=(
                    "queued"
                    if settings.DOCUMENT_AUTO_CONVERT and feature_enabled("knowledge", app)
                    else "quarantined"
                ),
                conversion_actor=actor,
            )
            audit(
                actor,
                "document.uploaded",
                doc.pk,
                app.product.portfolio.organization,
                details={"application_id": str(app.pk), "status": doc.status},
            )
        return doc
    except Exception:
        target.unlink(missing_ok=True)
        raise


@login_required
@require_http_methods(["GET", "POST"])
def documents(request, pk):
    app, grant = application_for(request.user, pk)
    can_upload = (
        grant.role in {"owner", "contributor"}
        and intake_enabled()
        and feature_enabled("document_uploads", app)
    )
    form = DocumentForm(request.POST or None, request.FILES or None)
    if request.method == "POST":
        if not can_upload:
            raise PermissionDenied
        if form.is_valid():
            saved = 0
            for upload in form.cleaned_data["file"]:
                try:
                    store_document(request.user, pk, upload)
                    saved += 1
                except (forms.ValidationError, OSError):
                    messages.error(
                        request,
                        "A file could not be stored. Successfully uploaded files are listed below.",
                    )
                    break
            if saved:
                messages.success(
                    request,
                    f"Uploaded {saved} document(s). "
                    "Markdown conversion runs automatically when Knowledge is enabled.",
                )
            return redirect("application", pk=pk)
    query = Document.objects.filter(application=app).exclude(status="deleted")
    search = request.GET.get("q", "").strip()[:120]
    if search:
        query = query.filter(name__icontains=search)
    return render(
        request,
        "documents.html",
        {
            "application": app,
            "grant": grant,
            "can_upload": can_upload,
            "intake_enabled": intake_enabled(),
            "uploads_enabled": feature_enabled("document_uploads", app),
            "form": form,
            "query": search,
            "conversion_pending": query.filter(status__in=["queued", "converting"]).exists(),
            "document_count": Document.objects.filter(application=app)
            .exclude(status="deleted")
            .count(),
            "page": Paginator(query, 20).get_page(request.GET.get("page")),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def document_detail(request, pk, document_id):
    app, grant = application_for(request.user, pk)
    try:
        document = Document.objects.exclude(status="deleted").get(pk=document_id, application=app)
    except Document.DoesNotExist:
        raise Http404 from None
    if request.method == "POST":
        from .workbench import access

        access(request.user, pk, "knowledge", write=True)
        with transaction.atomic():
            locked = Document.objects.select_for_update().get(pk=document.pk)
            if locked.status in {"quarantined", "failed"}:
                locked.status = "queued"
                locked.conversion_actor = request.user
                locked.conversion_error = ""
                locked.save(update_fields=["status", "conversion_actor", "conversion_error"])
                audit(
                    request.user,
                    "document.conversion_queued",
                    locked.pk,
                    app.product.portfolio.organization,
                )
        messages.success(request, "Document queued for Markdown conversion.")
        return redirect("document-detail", pk=pk, document_id=document_id)
    entry = KnowledgeEntry.objects.filter(document=document, active=True).first()
    download = request.GET.get("download")
    if download in {"markdown", "graph"}:
        if document.status != "ready" or not entry or not feature_enabled("knowledge", app):
            raise Http404
        if download == "markdown":
            body, content_type, extension = entry.content, "text/markdown; charset=utf-8", "md"
        else:
            body = json.dumps(
                {
                    "schema_version": 1,
                    "organization_id": str(app.organization_id),
                    "application_id": str(app.pk),
                    "document_id": str(document.pk),
                    "source_name": document.name,
                    "source_sha256": document.sha256,
                    "knowledge_id": str(entry.pk),
                    "markdown_sha256": entry.digest,
                    "converter": document.converter,
                    "markdown": entry.content,
                },
                indent=2,
            )
            content_type, extension = "application/json", "json"
        response = HttpResponse(body, content_type=content_type)
        response["Content-Disposition"] = f'attachment; filename="{document.pk}.{extension}"'
        return response
    return render(
        request,
        "document_detail.html",
        {
            "application": app,
            "document": document,
            "entry": entry,
            "can_delete": grant.role == "owner"
            or (grant.role == "contributor" and document.uploaded_by_id == request.user.pk),
            "scan_required": settings.DOCUMENT_SCAN_REQUIRED,
            "can_process": grant.role in {"owner", "contributor"}
            and feature_enabled("knowledge", app),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def document_delete(request, pk, document_id):
    app, grant = application_for(request.user, pk)
    doc = get_object_or_404(
        Document.objects.exclude(status="deleted"), pk=document_id, application=app
    )
    if not (
        grant.role == "owner"
        or (grant.role == "contributor" and doc.uploaded_by_id == request.user.pk)
    ):
        raise PermissionDenied
    if request.method == "POST":
        try:
            with transaction.atomic():
                locked = Document.objects.select_for_update().get(pk=doc.pk)
                target = (
                    Path(settings.BASE_DIR)
                    / ".runtime/documents"
                    / str(app.organization_id)
                    / str(app.pk)
                    / f"{doc.pk}.quarantine"
                )
                target.unlink(missing_ok=True)
                KnowledgeEntry.objects.filter(document=locked).delete()
                locked.status = "deleted"
                locked.conversion_error = ""
                locked.save(update_fields=["status", "conversion_error"])
                audit(
                    request.user, "document.deleted", locked.pk, app.product.portfolio.organization
                )
        except OSError:
            messages.error(request, "The file is currently in use. Please retry deletion shortly.")
            return redirect("document-delete", pk=pk, document_id=document_id)
        messages.success(request, "Document and its converted Markdown deleted.")
        return redirect("application", pk=pk)
    return render(request, "document_delete.html", {"application": app, "document": doc})
