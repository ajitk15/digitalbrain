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
import logging
import time
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import models, transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .connector_kinds import KINDS, MAX_BODY_CHARACTERS, MAX_RECORDS, kind_for
from .models import Connector, KnowledgeEntry
from .services import audit, feature_enabled
from .workbench import access, add_knowledge

logger = logging.getLogger("digitalbrain.connectors")


def credential(app, kind):
    """The mounted secret for one connector kind, or "" when none is mounted.

    One file per kind per application, matching the platform's secret rule. Two
    connectors of the same kind in one application therefore share a credential -
    a deliberate consequence of that rule, not an oversight.
    """
    from .secrets import application_secret

    return application_secret(app, kind)


def credential_location(app, kind):
    """Where this application's secret for one kind belongs, and whether it is there.

    The value is never included. A screen that tells someone to mount a file
    without saying which file, in which directory, is a screen they cannot act
    on - which is what sent people to the filesystem to guess.
    """
    from .secrets import manageable_here, source_of

    name = f"{kind}_{app.pk}"
    directory = str(settings.SECRET_DIRECTORY)
    return {
        "name": name,
        "directory": directory,
        "path": str(Path(directory) / name),
        "mounted": bool(credential(app, kind)),
        "source": source_of(app, kind),
        "manageable": manageable_here(),
    }


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
        pruned = prune(locked, app, kind, records)
        finish(locked, "ok", count, started, "")
        audit(
            user,
            "connector.synced",
            locked.pk,
            app.product.portfolio.organization,
            details={
                "kind": locked.kind,
                "imported": count,
                "seen": len(records),
                "pruned": pruned,
            },
        )
    return count


def prune(connector, app, kind, records):
    """Retire knowledge for records this connector no longer returns.

    Three conditions, and all three are load-bearing:

    * The owner ticked `prune_missing`. Absence only means deletion when the
      filter returns the whole set; under `updated >= -30d` it means the ticket
      is old. Only the person who wrote the filter knows which they have.
    * The result was not truncated. At `MAX_RECORDS` the set is a page, not an
      answer, and everything past the cap would look deleted.
    * No other enabled connector shares this namespace. Two connectors onto one
      Jira site with different filters would otherwise retire each other's
      imports, each correctly concluding the records are not in *its* results.

    Retiring is `active=False`, the same supersede the loop above performs. The
    rows stay for plan hashes and audit evidence; nothing is deleted.
    """
    if not connector.prune_missing or len(records) >= MAX_RECORDS:
        return 0
    try:
        namespace = kind.namespace(connector.config)
    except KeyError:
        return 0
    rivals = Connector.objects.filter(
        application=app, kind=connector.kind, enabled=True
    ).exclude(pk=connector.pk)
    for rival in rivals:
        try:
            if kind.namespace(rival.config) == namespace:
                return 0
        except KeyError:
            continue
    return KnowledgeEntry.objects.filter(
        application=app, active=True, source__startswith=namespace
    ).exclude(source__in=[record.url for record in records]).update(active=False)


def finish(connector, status, count, started, error):
    """Record the outcome on the row, so a failure leaves a visible trace."""
    Connector.objects.filter(pk=connector.pk).update(
        last_status=status,
        last_error=error[:500],
        last_count=count if status == "ok" else connector.last_count,
        last_synced_at=timezone.now() if status == "ok" else connector.last_synced_at,
        # Always, on both paths. The schedule counts from the attempt, so a
        # connector whose instance is down waits its interval rather than being
        # retried on every tick of the lane.
        last_attempt_at=timezone.now(),
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
                    "schedule": dict(SYNC_INTERVALS).get(row.sync_interval_minutes, ""),
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
    schedule = forms_schedule(request, connector)
    if request.method == "POST" and form.is_valid() and name_field["value"]:
        config = {field: value for field, value in form.cleaned_data.items()}
        with transaction.atomic():
            if connector is None:
                connector = Connector(application=app, kind=key, created_by=request.user)
            connector.name = name_field["value"]
            connector.config = config
            connector.sync_interval_minutes = schedule["interval"]
            connector.prune_missing = schedule["prune"]
            try:
                connector.full_clean(exclude=["created_by"])
                connector.save()
            except ValidationError as failure:
                messages.error(request, " ".join(failure.messages))
                return redirect("connectors", pk=pk)
            if key == "github" and feature_enabled("code_graph", app):
                from .code_graph_ingest import showcase_repository

                showcase_repository(request.user, app, config["repository"])
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
            "schedule": schedule,
            "secret_name": f"{key}_{app.pk}",
            "credential": bool(credential(app, key)),
            "location": credential_location(app, key),
        },
    )


def forms_schedule(request, connector):
    """How often this connector imports by itself, and whether it prunes.

    Kept beside the name rather than inside the kind's form: every system is
    imported on the same schedule mechanism, so repeating these two fields in
    three per-kind forms would be three places for them to drift.
    """
    if request.method == "POST":
        try:
            interval = int(request.POST.get("interval") or 0)
        except ValueError:
            interval = 0
        if interval not in dict(SYNC_INTERVALS):
            interval = 0
        prune_missing = request.POST.get("prune") == "on"
    else:
        interval = connector.sync_interval_minutes if connector else 0
        prune_missing = connector.prune_missing if connector else False
    return {
        "interval": interval,
        "prune": prune_missing,
        "choices": [
            {"value": value, "label": label, "selected": value == interval}
            for value, label in SYNC_INTERVALS
        ],
    }


def forms_name(request, connector, kind):
    """The connector's display name, kept out of the kind-specific form."""
    if request.method == "POST":
        value = (request.POST.get("name") or "").strip()[:120]
    else:
        value = connector.name if connector else kind.label
    return {"value": value, "error": "" if value or request.method == "GET" else "Enter a name."}


#: What an owner may choose on the form. Nothing shorter than a quarter of an
#: hour: an import is a full pull of up to a hundred records against somebody
#: else's rate limit, and a ticket board does not change faster than a person
#: can read it.
SYNC_INTERVALS = [
    (0, "Manual only"),
    (15, "Every 15 minutes"),
    (60, "Every hour"),
    (240, "Every 4 hours"),
    (1440, "Once a day"),
]


def due_connector():
    """The next enabled connector whose interval has elapsed, or None.

    Ordered by how long it has been waiting, so a single slow instance cannot
    starve every other connector behind it.
    """
    now = timezone.now()
    candidates = Connector.objects.filter(enabled=True, sync_interval_minutes__gt=0).order_by(
        models.F("last_attempt_at").asc(nulls_first=True)
    )
    for connector in candidates:
        if connector.last_attempt_at is None:
            return connector
        if connector.last_attempt_at <= now - timedelta(minutes=connector.sync_interval_minutes):
            return connector
    return None


def process_next_connector():
    """Run one scheduled import. Returns True when there was work to do.

    **An unattended run is still somebody's run.** It is performed as the person
    who created the connector, and `sync` re-checks their owner grant exactly as
    it does for a button press - so revoking a grant stops the schedule with no
    extra code, the same property API tokens have. A connector whose creator has
    been removed or demoted stops and says so on the row rather than falling back
    to some privileged identity, because there isn't one.
    """
    connector = due_connector()
    if connector is None:
        return False
    started = time.monotonic()
    if connector.created_by is None or not connector.created_by.is_active:
        finish(
            connector,
            "failed",
            0,
            started,
            "Scheduled import needs the person who created this connector. "
            "Import it by hand, or recreate it.",
        )
        return True
    try:
        count = sync(connector.created_by, connector.pk, connector.application_id)
    except PermissionDenied:
        finish(
            connector,
            "failed",
            0,
            started,
            "Scheduled import stopped: the person who created this connector no "
            "longer owns this application.",
        )
    except (ValidationError, Http404):
        # sync already recorded the reason on the row, or the application is
        # gone. Either way the lane keeps going.
        pass
    else:
        logger.info(
            "scheduled import finished",
            extra={
                "event": "connector_scheduled_sync",
                "connector_id": str(connector.pk),
                "kind": connector.kind,
                "imported": count,
            },
        )
    return True
