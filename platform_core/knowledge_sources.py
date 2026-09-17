"""Sources that are remembered, re-checked, and resynchronised on request.

A link import resolves a directory into files. Until now that was the end of it:
the address someone pasted was used once and discarded, so nothing recorded that
twenty-seven documents shared an origin, and nothing could say whether that
origin had moved on.

A `KnowledgeSource` is that address, kept. Three things happen to it.

* **Registration** - the first import of an address creates it, and a later
  import of the same address adds to it rather than starting a rival.
* **Checking** - a scheduled pass reads one cheap signal per source and records
  whether the origin has moved. It downloads nothing and changes no document.
* **Resynchronising** - re-resolving the source and queueing what changed. This
  happens **only when a person asks for it.**

That last rule is the point of the whole module. Detecting drift is free and
safe, so it is automatic. Acting on drift rewrites somebody's knowledge base -
it can add documents, supersede content a published graph version cites, and
spend a provider's rate limit - so it waits to be asked, in the same way Code
Factory describes work and stops.
"""

import logging

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from .models import Document, KnowledgeSource
from .policy import application_for
from .services import audit

logger = logging.getLogger(__name__)

#: How long a source may go unchecked before the scheduled pass looks again.
#: An hour, because the signal costs one request and GitHub allows sixty of them
#: an hour without a credential - so a handful of sources stay well inside the
#: budget, and a deployment with many of them should mount one.
CHECK_INTERVAL_MINUTES = 60


def readable_name(provider, raw):
    """A label for the source list: the part of the address that identifies it."""
    from urllib.parse import urlparse

    parsed = urlparse(raw)
    parts = [segment for segment in (parsed.path or "").split("/") if segment]
    if provider == "github" and len(parts) >= 2:
        owner, repo = parts[0], parts[1]
        if len(parts) > 4 and parts[2] == "tree":
            return f"{owner}/{repo} · {'/'.join(parts[4:])}"[:240]
        return f"{owner}/{repo}"[:240]
    if parts:
        return f"{parsed.hostname or ''}/{parts[-1]}"[:240]
    return (parsed.hostname or raw)[:240]


def register(user, app, provider, raw):
    """The source an import belongs to, created once and reused after that.

    Keyed on the address so importing the same directory twice adds to one
    source. Two sources for one origin would drift apart independently and
    disagree about the same files.
    """
    source, created = KnowledgeSource.objects.get_or_create(
        application=app,
        url=raw[:2000],
        defaults={
            "added_by": user,
            "provider": provider,
            "name": readable_name(provider, raw),
        },
    )
    if created:
        audit(user, "source.registered", source.pk, app.product.portfolio.organization)
    return source


def signal(source, app):
    """The cheap change indicator for this source, or "" when there is none.

    An empty string means "cannot tell from here", which is different from "has
    not changed" and is recorded as such: a source that cannot be checked must
    not read as a source that is in sync.
    """
    if source.provider == "github":
        from .fetching import normalise
        from .github_sources import latest_commit, parse
        from .link_sources import github_token

        try:
            _kind, owner, repo, ref, path = parse(normalise(source.url))
        except Exception:
            return ""
        return latest_commit(owner, repo, ref or "main", path, github_token(app))
    if source.provider == "sharepoint":
        from .fetching import normalise
        from .link_sources import sharepoint_secret
        from .sharepoint import folder_signal

        return folder_signal(normalise(source.url), sharepoint_secret(app))
    # A plain page has no indicator that costs less than fetching it, and
    # fetching it is what resynchronising does. It is checked when asked.
    return ""


def check(source, app):
    """Record whether the origin has moved. Downloads nothing, changes nothing."""
    current = signal(source, app)
    now = timezone.now()
    fields = {"last_checked_at": now}
    if not current:
        fields["status"] = KnowledgeSource.Status.UNREACHABLE
        fields["drift_summary"] = (
            "This source could not be checked automatically. Resync to compare it."
        )
    elif not source.fingerprint:
        # First sight of a signal. Nothing to compare against, so record it and
        # call the source in sync rather than announcing a change nobody made.
        fields["status"] = KnowledgeSource.Status.READY
        fields["fingerprint"] = current
        fields["drift_summary"] = ""
    elif current != source.fingerprint:
        fields["status"] = KnowledgeSource.Status.STALE
        fields["drift_summary"] = (
            "The origin has changed since this source was last synchronised. "
            "Resync to bring the changes in."
        )
    else:
        fields["status"] = KnowledgeSource.Status.READY
        fields["drift_summary"] = ""
    KnowledgeSource.objects.filter(pk=source.pk).update(**fields)
    return fields["status"]


def process_next_source():
    """One scheduled check. Reads a signal, writes a status, and stops there.

    Deliberately does not resynchronise anything it finds. The whole value of
    checking automatically is that it is safe to do so, and it is safe precisely
    because it never acts.
    """
    from datetime import timedelta

    due = timezone.now() - timedelta(minutes=CHECK_INTERVAL_MINUTES)
    source = (
        KnowledgeSource.objects.filter(application__active=True)
        .filter(models_q_unchecked_or_due(due))
        .select_related("application__product__portfolio__organization")
        .order_by("last_checked_at")
        .first()
    )
    if source is None:
        return False
    try:
        check(source, source.application)
    except Exception as failure:
        KnowledgeSource.objects.filter(pk=source.pk).update(
            last_checked_at=timezone.now(),
            status=KnowledgeSource.Status.UNREACHABLE,
            drift_summary="This source could not be checked automatically.",
        )
        logger.warning(
            "source_check_failed",
            extra={
                "event": "source_check_failed",
                "source_id": str(source.pk),
                "exception_type": type(failure).__name__,
            },
        )
    return True


def models_q_unchecked_or_due(due):
    from django.db.models import Q

    return Q(last_checked_at__isnull=True) | Q(last_checked_at__lt=due)


def resync(user, app_id, source_id):
    """Bring a source up to date. Only ever reached because somebody asked.

    Returns (queued, orphaned): how many documents were queued for download, and
    how many are no longer present at the origin.

    The comparison itself lives in `link_sources.submit`, which is the one place
    that resolves an address to a set of files. Doing it here would mean
    resolving twice - two walks of the same directory, and on GitHub twice the
    rate limit - to answer one question.
    """
    from .link_sources import submit

    app, grant = application_for(user, app_id)
    if grant.role not in {"owner", "contributor"}:
        raise PermissionDenied
    source = KnowledgeSource.objects.filter(pk=source_id, application=app).first()
    if source is None:
        raise ValidationError("That source no longer exists.")

    notes = []
    created = submit(user, app_id, source.url, notes, source=source)
    orphaned = (
        Document.objects.filter(source=source, orphaned=True)
        .exclude(status="deleted")
        .count()
    )
    with transaction.atomic():
        KnowledgeSource.objects.filter(pk=source.pk).update(
            status=KnowledgeSource.Status.READY,
            fingerprint=signal(source, app) or source.fingerprint,
            drift_summary=(
                f"{orphaned} document(s) are no longer at the origin." if orphaned else ""
            ),
            last_checked_at=timezone.now(),
            last_synced_at=timezone.now(),
        )
        audit(
            user,
            "source.resynchronised",
            source.pk,
            app.product.portfolio.organization,
            details={"queued": len(created), "orphaned": orphaned},
        )
    return len(created), orphaned


def forget(user, app_id, source_id, *, remove_documents=False):
    """Stop tracking an origin. Returns (kept, removed).

    Removing the documents uses the same path the delete button on a single
    document uses - the stored file unlinked, the converted entry removed, the
    row marked deleted rather than erased - so an audit trail survives a source
    being forgotten. Nothing here erases history.
    """
    from .documents import document_path
    from .models import Document, KnowledgeEntry

    app, grant = application_for(user, app_id)
    if grant.role != "owner":
        raise PermissionDenied
    source = KnowledgeSource.objects.filter(pk=source_id, application=app).first()
    if source is None:
        raise ValidationError("That source no longer exists.")

    files = list(Document.objects.filter(source=source).exclude(status="deleted"))
    removed = 0
    with transaction.atomic():
        if remove_documents:
            for document in files:
                path = document_path(app, document.pk)
                path.unlink(missing_ok=True)
                KnowledgeEntry.objects.filter(document=document).delete()
                Document.objects.filter(pk=document.pk).update(
                    status="deleted", conversion_error=""
                )
                audit(user, "document.deleted", document.pk, app.product.portfolio.organization)
                removed += 1
        audit(
            user,
            "source.forgotten",
            source.pk,
            app.product.portfolio.organization,
            details={"kept": 0 if remove_documents else len(files), "removed": removed},
        )
        # The documents' own rows survive either way; `source` is SET_NULL, so
        # the ones that were kept simply stop belonging to an origin.
        source.delete()
    return (0 if remove_documents else len(files)), removed
