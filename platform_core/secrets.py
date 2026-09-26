"""Per-application credentials an owner can manage from the browser.

Until now every credential was mounted by an operator, and CLAUDE.md said in so
many words that a secret is never entered in a form. That rule bought two things:
a secret never reached the database, the audit log or a backup, and it never
travelled in a request body. **A deliberate decision has traded the second for
self-service.** The first is kept exactly: what an owner types here is written to
a file with owner-only permissions and read back through the same `read_secret`
every other caller already used. Nothing about a credential is stored in the
database except who set it, when, and a digest that cannot be reversed.

Two directories, in this order:

* `secret_directory` - what an operator mounts. Read-only in production, and it
  **wins**. A deployment that projects credentials from a secret manager is not
  overridden by something typed into a form, which is the same precedence
  `claude_use_host_login` already obeys.
* `managed_secret_directory` - what this module writes. Optional: leave it unset
  and browser management is simply unavailable, which is what every existing
  deployment gets until someone provisions a writable volume for it.

Names are never taken from a request. The form posts a key from `MANAGEABLE`, and
the file name is built here as `<key>_<application id>` - so a name cannot escape
the directory and one application cannot address another's file.
"""

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import transaction
from django.shortcuts import redirect, render
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from digitalbrain.configuration import read_secret

from .models import ManagedCredential
from .policy import application_for
from .services import audit

#: Longer than any provider token, short enough that a pasted file is refused.
MAX_SECRET_LENGTH = 8192


@dataclass(frozen=True)
class Manageable:
    """One credential an application can hold, as the Credentials screen shows it."""

    key: str
    label: str
    icon: str
    #: Which part of the product stops working without it.
    purpose: str
    #: How to obtain it, in one line. The screen is useless without this.
    hint: str


#: Application-scoped credentials only. `django_secret_key` and `database_password`
#: are deliberately absent: the process cannot reach this screen without them, so
#: offering to set them here would be offering to lock the platform out of itself.
MANAGEABLE = {
    entry.key: entry
    for entry in (
        Manageable(
            "openai",
            "OpenAI",
            "sliders",
            "Chat answers and graph enrichment when the provider is OpenAI.",
            "An API key from platform.openai.com, beginning sk-.",
        ),
        Manageable(
            "claude",
            "Claude",
            "sliders",
            "Chat answers and Code Factory runs when the provider is Claude.",
            "An API key from console.anthropic.com, or the token from claude setup-token.",
        ),
        Manageable(
            "github",
            "GitHub (read)",
            "github",
            "Issue imports, link imports and Code Graph indexing.",
            "A fine-grained token with read access to repository contents.",
        ),
        Manageable(
            "github_write",
            "GitHub (write)",
            "github",
            "Opening a pull request at the end of a Code Factory run.",
            "A separate token with pull request write access. Deliberately not the read token.",
        ),
        Manageable(
            "jira",
            "Jira",
            "jira",
            "Importing issues through a Jira connector.",
            "A Cloud API token from id.atlassian.com, or a Data Center personal access token.",
        ),
        Manageable(
            "servicenow",
            "ServiceNow",
            "servicenow",
            "Importing records through a ServiceNow connector.",
            "The service account password, or the OAuth client secret.",
        ),
        Manageable(
            "sharepoint",
            "SharePoint",
            "document",
            "Importing documents from a SharePoint site.",
            "The client secret for the app registration named in the deployment configuration.",
        ),
    )
}


def managed_directory():
    """Where browser-managed credentials are written, or None when unconfigured."""
    directory = getattr(settings, "MANAGED_SECRET_DIRECTORY", "")
    return Path(directory) if directory else None


def manageable_here():
    """Whether this deployment offers credential management at all."""
    return managed_directory() is not None


def file_name(key, app):
    if key not in MANAGEABLE:
        raise ValidationError("Unknown credential.")
    return f"{key}_{app.pk}"


def application_secret(app, key):
    """This application's credential for one system, or "" when it has none.

    The operator's mount is consulted first, so a deployment that projects
    credentials from a secret manager behaves exactly as it did before this
    module existed.
    """
    name = f"{key}_{app.pk}"
    for directory in (settings.SECRET_DIRECTORY, managed_directory()):
        if directory is None:
            continue
        try:
            return read_secret(directory, name)
        except ImproperlyConfigured:
            continue
    return ""


def source_of(app, key):
    """Where this credential comes from: "operator", "managed" or "" for neither.

    Presence only - the value is never read to answer this, so a screen asking
    what is configured never loads a secret into memory to find out.
    """
    name = f"{key}_{app.pk}"
    if (Path(settings.SECRET_DIRECTORY) / name).is_file():
        return "operator"
    directory = managed_directory()
    if directory is not None and (directory / name).is_file():
        return "managed"
    return ""


def digest_of(name, value):
    """A short digest for display, salted with the file name.

    The name carries the application id, so the same token set on two
    applications produces two different digests. It confirms *which* credential
    is loaded without being a way to recognise one across applications.
    """
    return hashlib.sha256(f"{name}\x00{value}".encode()).hexdigest()[:12]


def clean_secret(value):
    value = (value or "").strip()
    if not value:
        raise ValidationError("Enter the credential.")
    if len(value) > MAX_SECRET_LENGTH:
        raise ValidationError("That is longer than any credential this platform accepts.")
    if any(character in value for character in "\r\n"):
        raise ValidationError("A credential is a single line. Paste the value on its own.")
    return value


def owner_access(user, app_id):
    app, grant = application_for(user, app_id, owner=True)
    return app, grant


def write_secret(directory, name, value):
    """Write one credential to disk, owner-only, replacing any previous value.

    Written to a temporary file in the same directory and moved into place, so a
    reader never sees a half-written credential, and created at mode 0600 rather
    than chmod-ed afterwards, so it is never briefly readable by anyone else.
    """
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f".{name}.writing"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
        os.replace(temporary, directory / name)
    finally:
        if temporary.exists():
            temporary.unlink()


def set_credential(user, app_id, key, value):
    """Record one credential for one application. Returns the application."""
    app, _ = owner_access(user, app_id)
    directory = managed_directory()
    if directory is None:
        raise ValidationError(
            "This deployment has no directory for browser-managed credentials. "
            "An operator sets managed_secret_directory to a writable volume."
        )
    if key not in MANAGEABLE:
        raise ValidationError("Unknown credential.")
    value = clean_secret(value)
    name = file_name(key, app)
    try:
        write_secret(directory, name, value)
    except OSError:
        raise ValidationError(
            "The credential directory could not be written. An operator has to "
            "make it writable by the service account."
        ) from None
    with transaction.atomic():
        ManagedCredential.objects.update_or_create(
            application=app,
            name=key,
            defaults={"digest": digest_of(name, value), "updated_by": user},
        )
        # The value is never an audit detail. What happened, to which credential,
        # by whom, is the whole record.
        audit(
            user,
            "credential.set",
            app.pk,
            app.product.portfolio.organization,
            details={"credential": key},
        )
    return app


def clear_credential(user, app_id, key):
    """Remove a browser-managed credential. An operator mount is never touched."""
    app, _ = owner_access(user, app_id)
    directory = managed_directory()
    if key not in MANAGEABLE:
        raise ValidationError("Unknown credential.")
    if directory is not None:
        try:
            (directory / file_name(key, app)).unlink(missing_ok=True)
        except OSError:
            raise ValidationError("The credential could not be removed.") from None
    with transaction.atomic():
        ManagedCredential.objects.filter(application=app, name=key).delete()
        audit(
            user,
            "credential.cleared",
            app.pk,
            app.product.portfolio.organization,
            details={"credential": key},
        )
    return app


def relevant(app, key):
    """Whether this application uses a credential, by what it is set up for.

    Jira and ServiceNow follow the connector kinds its owner chose; GitHub
    (write) follows Code Factory. GitHub (read) is also how link imports and Code
    Graph authenticate, so it stays. The registry is unchanged - this only
    decides which rows the screen offers.
    """
    from .services import connector_feature, feature_enabled

    if key in {"jira", "servicenow"}:
        return feature_enabled(connector_feature(key), app)
    if key == "github_write":
        return feature_enabled("code_factory", app)
    return True


def rows(app):
    """Every credential this application uses, with where it stands.

    A credential that is set is always listed, used or not, so it can be seen
    and cleared.
    """
    records = {row.name: row for row in ManagedCredential.objects.filter(application=app)}
    listed = []
    for entry in MANAGEABLE.values():
        source = source_of(app, entry.key)
        if not source and not relevant(app, entry.key):
            continue
        record = records.get(entry.key)
        listed.append(
            {
                "entry": entry,
                "source": source,
                # A file can be present and still unusable - wrong permissions on
                # POSIX, or empty. Reporting that as "not set" sent people looking
                # for a file that was already there.
                "usable": bool(source) and bool(application_secret(app, entry.key)),
                "file": f"{entry.key}_{app.pk}",
                # Provenance is only shown for the file this platform wrote. A row
                # left behind by a credential an operator has since mounted over
                # would otherwise claim authorship of a file it did not write.
                "record": record if source == "managed" else None,
            }
        )
    return listed


@login_required
@require_http_methods(["GET", "POST"])
@sensitive_post_parameters("secret")
def credentials(request, pk):
    """The Credentials screen: one row per system this application can reach.

    A real form per row, posting to this URL. Nothing here is fetched by script,
    so the screen works with JavaScript disabled like every other control.
    """
    app, grant = owner_access(request.user, pk)
    if request.method == "POST":
        key = request.POST.get("credential", "")
        try:
            if request.POST.get("action") == "clear":
                clear_credential(request.user, pk, key)
                messages.success(request, f"{MANAGEABLE[key].label} credential removed.")
            else:
                set_credential(request.user, pk, key, request.POST.get("secret", ""))
                messages.success(request, f"{MANAGEABLE[key].label} credential saved.")
        except ValidationError as failure:
            messages.error(request, " ".join(failure.messages))
        return redirect("credentials", pk=pk)
    return render(
        request,
        "credentials.html",
        {
            "application": app,
            "grant": grant,
            "rows": rows(app),
            "available": manageable_here(),
            "operator_directory": str(settings.SECRET_DIRECTORY),
        },
    )
