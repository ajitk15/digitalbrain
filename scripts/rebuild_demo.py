"""Build the CarePath demonstration workspace in one run - and only this:

 0. Settings: self-approval and demo reset switched on in config/local.toml
 1. Organization ACME
 2. Portfolio Integrated Care
 3. Product Care Coordination
 4. Engineering application CarePathDev
 5. Operations application CarePathOps
 6. Users: acmeadmin (owner of ACME and of both applications) and the platform
    administrator siteadmin@db.com, both with password demo123456789
 7. The connectors and their configuration
 8. Where the Jira and ServiceNow credential files go, and their names
 9. Knowledge from the local folder demo-artifacts/docs, built into a graph
10. Code from the local folder demo-artifacts/carepath, built into a code graph
11. Jira tickets imported into CarePathDev
12. ServiceNow tickets imported into CarePathOps

Run by `start-all.cmd -Demo`, or directly:

    .venv/Scripts/python.exe scripts/rebuild_demo.py

Idempotent: a second run reports what already exists and creates nothing new.
A password is set only when its user is created, never reset, and the whole
script refuses to run on a production instance. Nothing here deletes anything.
No model is called.
"""

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "digitalbrain.settings")

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from django.core.exceptions import ValidationError  # noqa: E402

from platform_core.models import (  # noqa: E402
    Application,
    ApplicationFeature,
    ApplicationGrant,
    Connector,
    Document,
    GraphRevision,
    Organization,
    OrganizationMember,
    Portfolio,
    Product,
    User,
)
from platform_core.services import (  # noqa: E402
    DEFAULT_CONNECTORS,
    connector_feature,
    features_for_purpose,
)

ROOT = Path(__file__).resolve().parent.parent
DOCS_FOLDER = ROOT / "demo-artifacts" / "docs"
CODE_FOLDER = ROOT / "demo-artifacts" / "carepath"

#: The demo's sign-in, for a demonstration instance on this machine only.
DEMO_USER = "acmeadmin"
DEMO_PASSWORD = "demo123456789"
#: The platform administrator, with the same password. Created here so a new
#: machine needs no interactive bootstrap; .env names it, as bootstrap_admin
#: requires.
SITE_ADMIN = "siteadmin@db.com"

#: (name, purpose, the AI purpose its first job needs)
APPLICATIONS = (
    ("CarePathDev", "engineering", "plan_drafting"),
    ("CarePathOps", "operations", "serviceops_triage"),
)

JIRA = "https://ajitk15.atlassian.net"
SERVICENOW = "https://dev401026.service-now.com"

#: (kind, name, config, sync every n minutes, retire what the filter stops returning)
#: The runbook's tables, as the connector form stores them.
CONNECTORS = {
    "CarePathDev": (
        (
            "jira",
            "Jira",
            {
                "base_url": JIRA,
                "auth": "cloud",
                "account_email": "ajitk15@gmail.com",
                "jql": "project = KAN ORDER BY updated DESC",
            },
            60,
            True,
        ),
    ),
    "CarePathOps": (
        (
            "servicenow",
            "ServiceNow incidents",
            {
                "base_url": SERVICENOW,
                "auth": "basic",
                "identity": "digibrain",
                "table": "incident",
                "query": "",
                "history_pages": None,
                "assignment_groups": "CAREOPS",
                "title_field": "short_description",
                "body_field": "description",
            },
            0,
            False,
        ),
        (
            "servicenow",
            "ServiceNow changes",
            {
                "base_url": SERVICENOW,
                "auth": "basic",
                "identity": "digibrain",
                "table": "change_request",
                "query": "business_service.name=CAREPATH_OPS^ORDERBYDESCsys_updated_on",
                "history_pages": 1,
                "assignment_groups": "",
                "title_field": "short_description",
                "body_field": "description",
            },
            0,
            False,
        ),
    ),
}

#: Which credential file each application's import needs, and what is in it.
CREDENTIAL_FILES = (
    ("CarePathDev", "jira", "the Atlassian API token for ajitk15@gmail.com"),
    ("CarePathOps", "servicenow", "the password of the ServiceNow user digibrain"),
)

#: How long to wait for conversion and indexing before leaving it to the server.
WAIT_SECONDS = 20 * 60


#: The two settings a one-person demo needs, written into config/local.toml.
#: allow_self_approval lets the person who starts a run approve its plan;
#: allow_demo_reset lets the platform administrator empty ACME between demos.
DEMO_FLAGS = ("allow_self_approval", "allow_demo_reset")


def demo_settings():
    """Switch the demo's settings on in the configuration file, line by line.

    Rewritten rather than round-tripped through TOML, as init_local.py does,
    so every other line and comment survives. The server reads the file when it
    starts, which `start-all.cmd -Demo` does right after this script.
    """
    path = Path(os.environ.get("DIGITAL_BRAIN_CONFIG", ROOT / "config" / "local.toml"))
    lines = path.read_text(encoding="utf-8").splitlines()
    for flag in DEMO_FLAGS:
        found = [i for i, line in enumerate(lines) if line.partition("=")[0].strip() == flag]
        if found and lines[found[0]].partition("=")[2].strip() == "true":
            print(f"found   {flag} = true")
            continue
        if found:
            lines[found[0]] = f"{flag} = true"
        else:
            lines.append(f"{flag} = true")
        print(f"enabled {flag} = true")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def step(number, title):
    print()
    print(f"== {number}. {title}")


def application(owner, product, name, purpose, ai_purpose):
    """One application, as the create form writes it."""
    from platform_core.ai import seed_application_ai
    from platform_core.models import AIConfiguration

    app, made = Application.objects.get_or_create(name=name, product=product)
    print(f"{'created' if made else 'found  '} application {name} ({purpose})")
    ApplicationGrant.objects.update_or_create(
        application=app, user=owner, defaults={"role": "owner", "can_approve": True}
    )
    for key, enabled in features_for_purpose(purpose).items():
        ApplicationFeature.objects.update_or_create(
            application=app, key=key, defaults={"enabled": enabled}
        )
    for kind in ("github", "jira", "servicenow"):
        ApplicationFeature.objects.update_or_create(
            application=app,
            key=connector_feature(kind),
            defaults={"enabled": kind in DEFAULT_CONNECTORS[purpose]},
        )
    AIConfiguration.objects.get_or_create(
        application=app,
        purpose=ai_purpose,
        defaults={
            "provider": "claude",
            "model": "claude-sonnet-5",
            "enabled": True,
            "input_rate": 3,
            "output_rate": 15,
            "configured_by": owner,
        },
    )
    seed_application_ai(owner, app, "claude")
    return app


def site_admin():
    """siteadmin@db.com as platform administrator, created once and never reset.

    Never elevates an account that already exists: bootstrap_admin refuses the
    same, and a demo build must not be a way round it.
    """
    from platform_core.models import AuditEvent

    user = User.objects.filter(username=SITE_ADMIN).first()
    if user is None:
        user = User(username=SITE_ADMIN, is_platform_admin=True)
        user.set_password(DEMO_PASSWORD)
        user.save()
        AuditEvent.objects.create(
            actor=user, action="platform.admin_bootstrapped", resource_id=str(user.pk)
        )
        print(f"created platform administrator {SITE_ADMIN}, password {DEMO_PASSWORD}")
    elif user.is_platform_admin:
        print(f"found   platform administrator {SITE_ADMIN} (password left as it is)")
    else:
        print(f"skipped {SITE_ADMIN} exists but is not a platform administrator; left as it is")
    env = ROOT / ".env"
    if not env.exists():
        env.write_text(f"SITE_ADMIN_USER_ID={SITE_ADMIN}\n", encoding="utf-8")
        print(f"created .env naming {SITE_ADMIN} as the site administrator")


def demo_user():
    user = User.objects.filter(username=DEMO_USER).first()
    if user is not None:
        print(f"found   user {DEMO_USER} (password left as it is)")
        return user
    user = User(username=DEMO_USER)
    # The demo's published password, set directly rather than through the
    # platform's password rules.
    user.set_password(DEMO_PASSWORD)
    user.save()
    print(f"created user {DEMO_USER}, password {DEMO_PASSWORD}")
    return user


def load_connectors(owner, built):
    for app_name, definitions in CONNECTORS.items():
        app = built[app_name]
        for kind, name, config, interval, prune in definitions:
            _, made = Connector.objects.get_or_create(
                application=app,
                name=name,
                defaults={
                    "kind": kind,
                    "config": config,
                    "enabled": True,
                    "sync_interval_minutes": interval,
                    "prune_missing": prune,
                    "created_by": owner,
                },
            )
            print(f"{'created' if made else 'found  '} {app_name}: connector {name}")


def credential_files(built):
    """Where each file goes, and whether it is there. Returns the ones missing."""
    from platform_core.connectors import credential

    folder = settings.CONNECTOR_SECRET_DIRECTORY
    if not folder:
        print("connector_secret_directory is not set in config/local.toml. Set it, for")
        print('example connector_secret_directory = "C:/DigitalBrain/secrets", and run again.')
    missing = []
    for app_name, name, holds in CREDENTIAL_FILES:
        path = Path(folder) / name if folder else Path(name)
        present = bool(credential(built[app_name], name))
        print(f"{'found  ' if present else 'MISSING'} {path}")
        print(f"        one line: {holds}")
        if not present:
            missing.append(app_name)
    return missing


def load_docs(owner, built):
    from django.core.files.uploadedfile import SimpleUploadedFile

    from platform_core.documents import store_folder

    files = sorted(path for path in DOCS_FOLDER.rglob("*") if path.is_file())
    paths = [path.relative_to(DOCS_FOLDER.parent).as_posix() for path in files]
    for app in built.values():
        uploads = [SimpleUploadedFile(path.name, path.read_bytes()) for path in files]
        try:
            _, added, _, unchanged = store_folder(owner, app.pk, uploads, json.dumps(paths))
        except Exception as error:  # noqa: BLE001 - reported, and the rest still run
            print(f"FAILED  {app.name}: {DOCS_FOLDER}: {error}")
            continue
        print(f"loaded  {app.name}: {added} new document(s), {unchanged} unchanged")


def load_code(owner, dev):
    from platform_core import code_graph_local
    from platform_core.code_graph_ingest import register_local
    from platform_core.models import CodeRepository

    if CodeRepository.objects.filter(
        application=dev, provider="local", retired_at__isnull=True
    ).exists():
        print(f"found   CarePathDev: {CODE_FOLDER}")
        return
    if not code_graph_local.enabled():
        print("skipped: code_graph_local_roots is not set in config/local.toml. Copy")
        print("        config/local.example.toml to config/local.toml and run again.")
        return
    try:
        register_local(owner, dev.pk, str(CODE_FOLDER))
    except ValidationError as error:
        print(f"FAILED  CarePathDev: {CODE_FOLDER}: {' '.join(error.messages)}")
        return
    print(f"loaded  CarePathDev: {CODE_FOLDER}, queued for indexing")


def build_graphs(built):
    """Conversion, the knowledge graph and the code index, then publish."""
    from platform_core.document_worker import code_graph_steps, graph_steps, intake_steps
    from platform_core.graphs import publish_revision, published_revision

    ids = [app.pk for app in built.values()]
    steps = (*intake_steps(), *graph_steps(), *code_graph_steps())
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        worked = False
        for work in steps:
            try:
                worked = bool(work()) or worked
            except Exception as error:  # noqa: BLE001 - one bad document must not stop the rest
                print(f"note    {work.__name__}: {error}")
                worked = True
        waiting = Document.objects.filter(
            application_id__in=ids, status__in=Document.IN_FLIGHT
        ).count()
        if not worked and not waiting:
            break
        if not worked:
            time.sleep(2)
    else:
        print("still busy; the server's worker will finish it. Run this again to publish.")
        return
    owner = User.objects.get(username=DEMO_USER)
    for app in built.values():
        current = published_revision(app.pk)
        if current is not None:
            print(f"found   {app.name}: graph version {current.number} published")
            continue
        latest = GraphRevision.objects.filter(application=app).order_by("-number").first()
        if latest is None:
            print(f"waiting {app.name}: no graph version yet")
            continue
        try:
            publish_revision(owner, app.pk, latest.number)
        except ValidationError as error:
            print(f"FAILED  {app.name}: {' '.join(error.messages)}")
            continue
        print(f"published {app.name}: graph version {latest.number}")


def import_tickets(owner, app, missing):
    from platform_core.connectors import sync as sync_connector

    if app.name in missing:
        print(f"skipped {app.name}: its credential file is missing (step 8)")
        return
    for connector in Connector.objects.filter(application=app, enabled=True):
        try:
            count = sync_connector(owner, connector.pk, app.pk)
        except Exception as error:  # noqa: BLE001 - reported, and the rest still run
            print(f"FAILED  {app.name}: {connector.name}: {error}")
            continue
        print(f"imported {app.name}: {connector.name}, {count} record(s) changed")


def main(argv=None):
    if settings.PRODUCTION:
        print("Refusing to build the demo on a production instance.")
        return 1

    step(0, "Settings")
    demo_settings()
    step(1, "Organization")
    org, made = Organization.objects.get_or_create(name="ACME")
    print(f"{'created' if made else 'found  '} organization ACME")
    step(2, "Portfolio")
    portfolio, made = Portfolio.objects.get_or_create(name="Integrated Care", organization=org)
    print(f"{'created' if made else 'found  '} portfolio Integrated Care")
    step(3, "Product")
    product, made = Product.objects.get_or_create(name="Care Coordination", portfolio=portfolio)
    print(f"{'created' if made else 'found  '} product Care Coordination")

    # The user comes first in the code because the applications need an owner;
    # it is reported as step 6, where it belongs in the list.
    owner = demo_user()
    OrganizationMember.objects.update_or_create(
        organization=org, user=owner, defaults={"is_admin": True}
    )
    built = {}
    for number, (name, purpose, ai_purpose) in zip((4, 5), APPLICATIONS, strict=True):
        step(number, f"{purpose.capitalize()} application")
        built[name] = application(owner, product, name, purpose, ai_purpose)
    step(6, "Users")
    print(f"{DEMO_USER} is owner (administrator) of ACME and owns CarePathDev and CarePathOps")
    site_admin()

    step(7, "Connectors")
    load_connectors(owner, built)
    step(8, "Credential files")
    missing = credential_files(built)
    step(9, "Knowledge from demo-artifacts/docs")
    load_docs(owner, built)
    step(10, "Code from demo-artifacts/carepath")
    load_code(owner, built["CarePathDev"])
    step(11, "Jira tickets into CarePathDev")
    import_tickets(owner, built["CarePathDev"], missing)
    step(12, "ServiceNow tickets into CarePathOps")
    import_tickets(owner, built["CarePathOps"], missing)

    print()
    print("== Building the knowledge and code graphs (no model is called)")
    build_graphs(built)
    print()
    print(f"Sign in as {DEMO_USER}. Missing credential files: put them where step 8 says")
    print("and run this again; it imports what it could not before." if missing else "Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
