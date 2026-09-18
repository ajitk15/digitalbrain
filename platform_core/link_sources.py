"""Importing knowledge from a link.

The design rule here is that a link becomes an ordinary Document. Once the bytes
are on disk, every existing guarantee applies unchanged: quarantine storage, the
scanner hook, offline MarkItDown conversion, the immutable KnowledgeEntry it
produces, graph regeneration, and deletion. Nothing about the conversion pipeline
learns that URLs exist, and MarkItDown is still never handed a URL.

Submitting a link therefore does not download anything. It records one Document
per file in `pending`, and the background worker fetches them - so a slow or dead
host cannot hold a request open, and the rail shows exactly what is queued, what
is downloading and what failed.
"""

import hashlib
import os

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from .documents import document_folder, document_path, intake_enabled
from .fetching import FetchError, fetch, normalise, resolve, suggested_name
from .github_sources import is_github
from .github_sources import plan as github_plan
from .models import Document
from .policy import application_for
from .services import audit, feature_enabled
from .sharepoint import configured as sharepoint_configured
from .sharepoint import download_url, is_sharepoint
from .sharepoint import plan as sharepoint_plan


def mounted(app, provider):
    """A per-application credential, when one is mounted. Absent is not an error."""
    from .secrets import application_secret

    return application_secret(app, provider)


def github_token(app):
    return mounted(app, "github")


def sharepoint_available(app):
    """True when this application could actually import from SharePoint."""
    return bool(sharepoint_configured() and mounted(app, "sharepoint"))


def sharepoint_secret(app):
    """The client secret for this application's SharePoint access.

    Mounting it is what enables SharePoint import for an application, so an
    application without one simply cannot reach SharePoint even though the
    deployment is configured for it.
    """
    secret = mounted(app, "sharepoint")
    if not secret:
        raise ValidationError(
            "SharePoint import is not enabled for this application. An owner mounts "
            f"the client secret as sharepoint_{app.pk}."
        )
    if not sharepoint_configured():
        raise ValidationError(
            "SharePoint is not configured for this deployment. An operator sets "
            "sharepoint_tenant and sharepoint_client_id."
        )
    return secret


def preview(user, app_id, raw, notes=None):
    """Resolve a link to the files it would import, without downloading them.

    `notes` collects anything worth telling the person about the resolution -
    today, that the importer stopped at the configured ceiling.
    """
    app, grant = application_for(user, app_id)
    if grant.role not in {"owner", "contributor"}:
        raise PermissionDenied
    if not intake_enabled() or not feature_enabled("document_uploads", app):
        raise ValidationError("Link import is disabled for this application.")

    parsed = normalise(raw)
    # Resolve now purely so an unreachable or blocked address is reported while the
    # user is still looking at the box. The authoritative check is the one in
    # fetch(), which re-resolves and pins the connection; this one is for feedback.
    resolve(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    if is_sharepoint(parsed):
        return "sharepoint", sharepoint_plan(parsed, sharepoint_secret(app), notes)
    if is_github(parsed):
        return "github", github_plan(parsed, github_token(app), notes)
    # A plain page is one file; its real name is only known once headers arrive,
    # so the path is used now and corrected after the download.
    return "link", [(suggested_name(parsed, ""), parsed.geturl())]


def submit(user, app_id, raw, notes=None, source=None):
    """Queue everything a link resolves to. Returns the created documents.

    `source` is the `KnowledgeSource` this import belongs to. A first import
    registers one; a resynchronisation passes the existing one back in. Either
    way this is the only place that knows which files an address resolves to,
    which is why the comparison against what is already stored belongs here and
    not in the caller: doing it anywhere else means resolving the address twice.

    `notes` is appended to rather than returned, so a caller that does not care
    passes nothing and a view that wants to show the person what happened passes
    a list. The importers already stop at `IMPORT_MAX_FILES` and say so there;
    this used to re-truncate to a second, lower number of its own, silently.
    """
    app, _ = application_for(user, app_id)
    origin, files = preview(user, app_id, raw, notes)
    if not files:
        raise ValidationError("That link contained nothing to import.")
    # The importers bound themselves, and this is the backstop for one that
    # forgets: a single paste must not be able to queue an unbounded amount of
    # downloading. Trimming here is reported rather than silent, because a
    # backstop that fires quietly is a bug nobody finds.
    ceiling = settings.IMPORT_MAX_FILES
    if len(files) > ceiling:
        if notes is not None:
            notes.append(f"Only the first {ceiling} of {len(files)} files were queued.")
        files = files[:ceiling]

    # A resynchronisation passes the source in; a fresh paste does not. The
    # difference matters at the end: finding nothing new is an error for someone
    # pasting a link they have already imported, and the ordinary answer for a
    # source that is simply up to date.
    resynchronising = source is not None
    if source is None:
        from .knowledge_sources import register

        source = register(user, app, origin, raw)

    created = []
    resolved = {url[:2000] for _name, url in files}
    with transaction.atomic():
        for name, url in files:
            existing = Document.objects.filter(
                application=app, source_url=url
            ).exclude(status="deleted")
            if existing.exists():
                # Adopted rather than skipped in silence. Documents imported
                # before sources were recorded have none, and a source that
                # cannot see the files it brought in cannot say what has since
                # gone missing from the origin.
                existing.filter(source__isnull=True).update(source=source)
                continue
            document = Document.objects.create(
                application=app,
                uploaded_by=user,
                name=name[:200],
                size=0,
                sha256="",
                status="pending",
                origin=origin,
                source_url=url[:2000],
                source=source,
            )
            created.append(document)
        # What this source brought in before and the origin no longer offers.
        # Marked, never removed: a file renamed upstream would otherwise delete
        # a document that a published graph version cites.
        under = Document.objects.filter(source=source).exclude(status="deleted")
        under.exclude(source_url__in=resolved).update(orphaned=True)
        under.filter(source_url__in=resolved, orphaned=True).update(orphaned=False)
        audit(
            user,
            "document.linked",
            app.pk,
            app.product.portfolio.organization,
            details={
                "origin": origin,
                "queued": len(created),
                "resolved": len(files),
                "source_id": str(source.pk),
            },
        )
    if not created and not resynchronising:
        raise ValidationError("Everything at that link has already been imported.")
    return created


def import_still_permitted(document):
    """Whether the uploader may still cause this application to fetch on its behalf."""
    from django.http import Http404

    from .workbench import access

    if document.uploaded_by_id is None or not document.uploaded_by.is_active:
        return False
    try:
        access(document.uploaded_by, document.application_id, "knowledge", write=True)
    except (PermissionDenied, Http404):
        return False
    return True


def download(document):
    """Fetch a pending document's bytes and hand it to the conversion queue.

    Runs on the worker thread. Any failure is recorded on the row rather than
    raised, so one unreachable link never stops the queue.
    """
    claimed = Document.objects.filter(pk=document.pk, status="pending").update(
        status="fetching", conversion_started_at=timezone.now()
    )
    if not claimed:
        return False

    # A queued import can sit here after the person who asked for it lost access,
    # and the fetch below spends the application's own GitHub or SharePoint
    # credential. Re-check the grant now rather than trusting the one that existed
    # when the link was pasted; the queue is not a way to outlive a revocation.
    if not import_still_permitted(document):
        Document.objects.filter(pk=document.pk, status="fetching").update(
            status="failed",
            conversion_error=(
                "Import cancelled: the person who added this link no longer has "
                "access to the application."
            ),
        )
        return True

    token = ""
    target = document.source_url
    try:
        if document.origin == "sharepoint":
            # Graph answers /content with a redirect, and redirects are never
            # followed, so a fresh pre-authenticated address is read instead. Doing
            # it now rather than at import time means it cannot expire in the queue.
            target, _ = download_url(document.source_url, sharepoint_secret(document.application))
        elif document.origin == "github":
            token = github_token(document.application)
        body, content_type, name, _ = fetch(
            target,
            headers={"Authorization": f"Bearer {token}"} if token else None,
        )
    except (FetchError, ValidationError) as failure:
        message = " ".join(getattr(failure, "messages", [str(failure)]))
        Document.objects.filter(pk=document.pk, status="fetching").update(
            status="failed", conversion_error=message[:500]
        )
        return True
    except Exception:
        Document.objects.filter(pk=document.pk, status="fetching").update(
            status="failed",
            conversion_error="The link could not be downloaded. Check it and try again.",
        )
        return True

    folder = document_folder(document.application)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = document_path(document.application, document.pk)
    # Same restrictive mode the upload path uses: originals stay owner-only.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as destination:
        destination.write(body)
    Document.objects.filter(pk=document.pk, status="fetching").update(
        # Keep the name the link resolved to unless the server offered a better one.
        name=(document.name if "." in document.name else name)[:200],
        size=len(body),
        sha256=hashlib.sha256(body).hexdigest(),
        status="queued",
        conversion_error="",
    )
    return True
