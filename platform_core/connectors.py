"""Read-only GitHub issue import. Credentials are mounted per application."""

import hashlib
import json
import re
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.db import transaction
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from digitalbrain.configuration import read_secret

from .models import Connector, KnowledgeEntry
from .services import audit, feature_enabled
from .workbench import access, add_knowledge


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ConnectorForm(forms.Form):
    repository = forms.RegexField(
        regex=r"^[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+$",
        max_length=200,
        help_text="GitHub owner/repository. Import reads the 100 most recently updated issues.",
    )
    enabled = forms.BooleanField(required=False, initial=True)

    def clean_repository(self):
        value = self.cleaned_data["repository"]
        if value.split("/")[1] in {".", ".."}:
            raise forms.ValidationError("Enter an owner/repository.")
        return value


def read_issues(repository, token):
    if not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", repository):
        raise ValidationError("Invalid repository.")
    request = Request(
        f"https://api.github.com/repos/{repository}/issues"
        "?state=all&sort=updated&direction=desc&per_page=100",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "Digital-Brain",
        },
    )
    try:
        with build_opener(NoRedirect()).open(request, timeout=15) as response:
            data = response.read(4 * 1024 * 1024 + 1)
        if len(data) > 4 * 1024 * 1024:
            raise ValueError
        items = json.loads(data)
        if not isinstance(items, list) or len(items) > 100:
            raise ValueError
        return items
    except (HTTPError, URLError, TimeoutError, ValueError, OSError):
        raise ValidationError(
            "GitHub could not be read. Check repository access, token permissions and connectivity."
        ) from None


def sync_issues(user, app_id):
    app, grant = access(user, app_id, "connectors")
    if grant.role != "owner" or not feature_enabled("knowledge", app):
        raise PermissionDenied
    connector = Connector.objects.get(application=app)
    if not connector.enabled:
        raise ValidationError("Enable this connector before importing.")
    token = read_secret(settings.SECRET_DIRECTORY, f"github_{app.pk}")
    items = read_issues(connector.repository, token)
    with transaction.atomic():
        app, current = access(user, app_id, "connectors")
        locked = Connector.objects.select_for_update().get(pk=connector.pk)
        if (
            current.role != "owner"
            or not locked.enabled
            or locked.repository != connector.repository
        ):
            raise PermissionDenied
        count = 0
        for item in items:
            if not isinstance(item, dict) or "pull_request" in item:
                continue
            if type(item.get("number")) is not int or not isinstance(item.get("title"), str):
                raise ValidationError("GitHub returned an unexpected issue record.")
            body = item.get("body") or ""
            if not isinstance(body, str):
                raise ValidationError("GitHub returned an unexpected issue body.")
            content = (item["title"] + "\n\n" + body)[:100000]
            source = f"https://github.com/{connector.repository}/issues/{item['number']}"
            digest = hashlib.sha256(content.encode()).hexdigest()
            existing = KnowledgeEntry.objects.filter(application=app, source=source, active=True)
            if existing.filter(digest=digest).exists():
                continue
            # Prior revisions stay immutable for plan hashes and audit evidence.
            existing.update(active=False)
            add_knowledge(user, app_id, item["title"][:200], content, source=source)
            count += 1
        locked.last_synced_at = timezone.now()
        locked.last_count = count
        locked.save(update_fields=["last_synced_at", "last_count"])
        audit(
            user,
            "connector.synced",
            locked.pk,
            app.product.portfolio.organization,
            details={"imported": count},
        )
    return count


@login_required
@require_http_methods(["GET", "POST"])
def connectors(request, pk):
    app, grant = access(request.user, pk, "connectors")
    if grant.role != "owner":
        raise PermissionDenied
    connector = Connector.objects.filter(application=app).first()
    form = ConnectorForm(
        request.POST or None,
        initial={
            "repository": connector.repository if connector else "",
            "enabled": connector.enabled if connector else True,
        },
    )
    if request.method == "POST":
        if request.POST.get("action") == "sync":
            try:
                count = sync_issues(request.user, pk)
                messages.success(request, f"Imported {count} new or updated issues into Knowledge.")
            except (ValidationError, ImproperlyConfigured, Connector.DoesNotExist) as error:
                text = "Configure the connector and mount its application token first."
                if isinstance(error, ValidationError):
                    text = " ".join(error.messages)
                messages.error(request, text)
            return redirect("connectors", pk=pk)
        if form.is_valid():
            with transaction.atomic():
                connector, _ = Connector.objects.update_or_create(
                    application=app, defaults=form.cleaned_data
                )
                audit(
                    request.user,
                    "connector.configured",
                    connector.pk,
                    app.product.portfolio.organization,
                )
            messages.success(request, "Connector settings saved.")
            return redirect("connectors", pk=pk)
    return render(
        request,
        "connectors.html",
        {
            "application": app,
            "form": form,
            "connector": connector,
            "secret_name": f"github_{app.pk}",
        },
    )
