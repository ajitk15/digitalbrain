"""Read-only imports from external systems. Credentials are mounted per application.

The kinds themselves live in connector_kinds.py; everything here is kind-agnostic,
so a new system is an entry in that registry rather than another import pipeline.

Two boundaries this file exists to hold:

* Every outbound call goes through `fetching.fetch` inside the adapters. Jira and
  ServiceNow take a base URL from the user, so an unchecked fetch here would be a
  request forgery tool aimed at whatever the server can reach.
* Imported text is knowledge, never instruction. It lands in KnowledgeEntry the
  same way an uploaded document does, and is quoted and verified the same way.
"""

import hashlib
import time

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from digitalbrain.configuration import read_secret

from .connector_kinds import KINDS, MAX_BODY_CHARACTERS, kind_for
from .models import Connector, KnowledgeEntry
from .services import audit, feature_enabled
from .workbench import access, add_knowledge


def credential(app, kind):
    """The mounted secret for one connector kind, or "" when none is mounted.

    One file per kind per application, matching the platform's secret rule. Two
    connectors of the same kind in one application therefore share a credential -
    a deliberate consequence of that rule, not an oversight.
    """
    try:
        return read_secret(settings.SECRET_DIRECTORY, f"{kind}_{app.pk}")
    except ImproperlyConfigured:
        return ""


def owner_access(user, app_id):
    app, grant = access(user, app_id, "connectors")
    if grant.role != "owner":
        raise PermissionDenied
    return app, grant


def sync(user, connector_id, app_id):
    """Import one connector's records as knowledge. Returns how many changed.

    The provider call happens outside the transaction; only the writes are inside
    it, so a slow or hanging instance never holds a database lock.
    """
    app, _ = owner_access(user, app_id)
    if not feature_enabled("knowledge", app):
        raise PermissionDenied
    connector = get_object_or_404(Connector, pk=connector_id, application=app)
    if not connector.enabled:
        raise ValidationError("Enable this connector before importing.")
    kind = kind_for(connector.kind)
    secret = credential(app, connector.kind)
    if kind.credential_required and not secret:
        raise ValidationError(
            f"Mount a credential for this application as {connector.kind}_{app.pk} "
            "in the configured secret directory before importing."
        )
    started = time.monotonic()
    try:
        records = kind.records(connector.config, secret)
    except ValidationError as failure:
        finish(connector, "failed", 0, started, " ".join(failure.messages))
        raise
    with transaction.atomic():
        app, current = access(user, app_id, "connectors")
        locked = Connector.objects.select_for_update().get(pk=connector.pk)
        # Re-check everything the run was authorised on: it may have changed while
        # the external system was answering.
        if current.role != "owner" or not locked.enabled or locked.config != connector.config:
            raise PermissionDenied
        count = 0
        for record in records:
            content = (record.title + "\n\n" + record.body)[:MAX_BODY_CHARACTERS]
            digest = hashlib.sha256(content.encode()).hexdigest()
            existing = KnowledgeEntry.objects.filter(
                application=app, source=record.url, active=True
            )
            if existing.filter(digest=digest).exists():
                continue
            # Prior revisions stay immutable for plan hashes and audit evidence.
            existing.update(active=False)
            add_knowledge(user, app_id, record.title, content, source=record.url)
            count += 1
        finish(locked, "ok", count, started, "")
        audit(
            user,
            "connector.synced",
            locked.pk,
            app.product.portfolio.organization,
            details={"kind": locked.kind, "imported": count, "seen": len(records)},
        )
    return count


def finish(connector, status, count, started, error):
    """Record the outcome on the row, so a failure leaves a visible trace."""
    Connector.objects.filter(pk=connector.pk).update(
        last_status=status,
        last_error=error[:500],
        last_count=count if status == "ok" else connector.last_count,
        last_synced_at=timezone.now() if status == "ok" else connector.last_synced_at,
        last_duration_ms=int((time.monotonic() - started) * 1000),
    )


@login_required
@require_http_methods(["GET", "POST"])
def connectors(request, pk):
    """List an application's connectors, and act on one of them."""
    app, grant = owner_access(request.user, pk)
    rows = Connector.objects.filter(application=app)
    if request.method == "POST":
        action = request.POST.get("action")
        connector = get_object_or_404(Connector, pk=request.POST.get("connector"), application=app)
        if action == "sync":
            try:
                count = sync(request.user, connector.pk, pk)
            except ValidationError as failure:
                messages.error(request, " ".join(failure.messages))
            else:
                messages.success(
                    request,
                    f"Imported {count} new or updated record(s) from {connector.name}."
                    if count
                    else f"{connector.name} had nothing new to import.",
                )
        elif action in {"enable", "disable"}:
            Connector.objects.filter(pk=connector.pk).update(enabled=action == "enable")
            audit(
                request.user,
                f"connector.{action}d",
                connector.pk,
                app.product.portfolio.organization,
            )
            messages.success(request, f"{connector.name} {action}d.")
        elif action == "delete":
            audit(
                request.user,
                "connector.deleted",
                connector.pk,
                app.product.portfolio.organization,
                details={"kind": connector.kind, "name": connector.name},
            )
            connector.delete()
            messages.success(
                request, f"{connector.name} removed. Knowledge it already imported is kept."
            )
        else:
            messages.error(request, "Unknown connector action.")
        return redirect("connectors", pk=pk)
    return render(
        request,
        "connectors.html",
        {
            "application": app,
            "grant": grant,
            "connectors": [
                {
                    "row": row,
                    "kind": KINDS.get(row.kind),
                    "target": KINDS[row.kind].identity(row.config) if row.kind in KINDS else "",
                    "credential": bool(credential(app, row.kind)),
                }
                for row in rows
            ],
            "kinds": list(KINDS.values()),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def connector_form(request, pk, connector_id=None):
    """Add a connector of a chosen kind, or edit an existing one."""
    app, grant = owner_access(request.user, pk)
    connector = (
        get_object_or_404(Connector, pk=connector_id, application=app) if connector_id else None
    )
    key = connector.kind if connector else request.GET.get("kind", "")
    if key not in KINDS:
        return render(
            request,
            "connector_choose.html",
            {"application": app, "grant": grant, "kinds": list(KINDS.values())},
        )
    kind = KINDS[key]
    initial = dict(connector.config) if connector else {}
    if connector:
        initial["name"] = connector.name
    form = kind.form(request.POST or None, initial=initial)
    name_field = forms_name(request, connector, kind)
    if request.method == "POST" and form.is_valid() and name_field["value"]:
        config = {field: value for field, value in form.cleaned_data.items()}
        with transaction.atomic():
            if connector is None:
                connector = Connector(application=app, kind=key, created_by=request.user)
            connector.name = name_field["value"]
            connector.config = config
            try:
                connector.full_clean(exclude=["created_by"])
                connector.save()
            except ValidationError as failure:
                messages.error(request, " ".join(failure.messages.get("__all__", failure.messages)))
                return redirect("connectors", pk=pk)
            audit(
                request.user,
                "connector.saved",
                connector.pk,
                app.product.portfolio.organization,
                details={"kind": key, "name": connector.name},
            )
        messages.success(request, f"{connector.name} saved.")
        return redirect("connectors", pk=pk)
    return render(
        request,
        "connector_form.html",
        {
            "application": app,
            "grant": grant,
            "kind": kind,
            "form": form,
            "connector": connector,
            "name_field": name_field,
            "secret_name": f"{key}_{app.pk}",
            "credential": bool(credential(app, key)),
        },
    )


def forms_name(request, connector, kind):
    """The connector's display name, kept out of the kind-specific form."""
    if request.method == "POST":
        value = (request.POST.get("name") or "").strip()[:120]
    else:
        value = connector.name if connector else kind.label
    return {"value": value, "error": "" if value or request.method == "GET" else "Enter a name."}
