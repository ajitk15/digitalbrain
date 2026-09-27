"""Build the CarePath demonstration workspace in one run.

Everything here is derived from demo-artifacts/demo-kit/demo-setup.md, which is
the runbook a person would follow by hand. Two applications, one per purpose:

* CarePathDev - Engineering: Jira tickets, the CarePath repository, Code Factory.
* CarePathOps - Operations: ServiceNow incidents and changes, ServiceOps triage.

Both read the same lifecycle documents, each into its own copy. In order:

1. Organization, portfolio, product, both applications, their features and AI
   configuration - what the create form and onboarding's connector question
   write.
2. The carepathdocs knowledge source for each, and the CarePath repository for
   CarePathDev.
3. The connectors, configured exactly as the runbook's tables say.
4. Credentials. Asked for on this console, one at a time, never echoed, and
   written by `secrets.set_credential` - the same code the Credentials screen
   runs, so the file, its mode and the audit record are identical. A credential
   that is already set is never asked for again. Press Enter to skip one.
5. One sync of every connector whose credential is present.
6. Document conversion, the structural graph and code indexing, driven here so
   the server does not have to be running. None of it calls a model.
7. The newest graph version published, where nothing is published yet - a
   version somebody published deliberately is left alone.

Idempotent. Run it twice and the second run reports what already existed rather
than creating a second ACME. Nothing here deletes anything.

    .venv/Scripts/python.exe scripts/rebuild_demo.py            everything
    .venv/Scripts/python.exe scripts/rebuild_demo.py --no-prompt  never ask for a secret
    .venv/Scripts/python.exe scripts/rebuild_demo.py --no-wait    stop after syncing
"""

import argparse
import getpass
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
    AIConfiguration,
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

DOCS = "https://github.com/ajitk15/carepathdocs/tree/main"
CODE = "ajitk15/carepath"

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

#: What each application needs set, in the order they are asked for.
CREDENTIALS = {
    "CarePathDev": ("claude", "jira", "github_write"),
    "CarePathOps": ("claude", "servicenow"),
}

#: How long to wait for conversion and indexing before leaving it to the server.
WAIT_SECONDS = 20 * 60


def section(title):
    print()
    print(f"== {title}")


def build(admin, product, name, purpose, ai_purpose):
    from platform_core.ai import seed_application_ai

    app, made = Application.objects.get_or_create(name=name, product=product)
    print(f"{'created' if made else 'found  '} application {name} ({purpose}, {app.pk})")
    # Owner, and explicitly able to approve, as the create form does. On this
    # instance allow_self_approval is on, which is what lets one operator run
    # the pipeline end to end; a real deployment grants a second person.
    ApplicationGrant.objects.update_or_create(
        application=app, user=admin, defaults={"role": "owner", "can_approve": True}
    )
    # What the create form writes for this purpose, then what onboarding's
    # connector question writes for its defaults.
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
            "configured_by": admin,
        },
    )
    # What the create form does next: every other purpose configured, and the
    # console-entered claude_default copied to this application's own file if
    # there is one. Copied, never read through - see CLAUDE.md.
    seed_application_ai(admin, app, "claude")
    return app


def sources(admin, built):
    from platform_core.code_graph_ingest import register
    from platform_core.link_sources import submit
    from platform_core.models import CodeRepository, KnowledgeSource

    for app in built.values():
        if KnowledgeSource.objects.filter(application=app, url=DOCS).exists():
            print(f"found   {app.name}: knowledge source carepathdocs")
        else:
            created = submit(admin, app.pk, DOCS, [])
            print(f"created {app.name}: knowledge source carepathdocs, {len(created)} queued")

    dev = built["CarePathDev"]
    if CodeRepository.objects.filter(application=dev, external_id=CODE.lower()).exists():
        print(f"found   CarePathDev: code repository {CODE}")
    else:
        repo = register(admin, dev.pk, CODE)
        print(f"created CarePathDev: code repository {repo.name}, queued for indexing")


def connectors(admin, built):
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
                    "created_by": admin,
                },
            )
            print(f"{'created' if made else 'found  '} {app_name}: connector {name}")


def credentials(admin, built, prompt):
    from platform_core.secrets import MANAGEABLE, manageable_here, set_credential, source_of

    if prompt and not sys.stdin.isatty():
        print("Not attached to a console, so no credential will be asked for.")
        prompt = False
    if prompt and not manageable_here():
        print("managed_secret_directory is not configured, so credentials cannot be")
        print(f"written from here. Put each file in {settings.SECRET_DIRECTORY} instead.")
        prompt = False
    # One answer per kind: the same Claude credential serves both applications,
    # written to each one's own file, so nobody pastes it twice.
    answers = {}
    missing = []
    for app_name, keys in CREDENTIALS.items():
        app = built[app_name]
        for key in keys:
            label = MANAGEABLE[key].label
            where = source_of(app, key)
            if where:
                print(f"found   {app_name}: {label} ({where})")
                continue
            if key == "claude" and settings.CLAUDE_USE_HOST_LOGIN and key not in answers:
                print(f"note    {app_name}: {label} not set; this machine's Claude login")
                print("        will be used (claude_use_host_login). Enter one to bill it here.")
            value = answers.get(key)
            if value is None and prompt:
                print(f"        {MANAGEABLE[key].hint}")
                value = getpass.getpass(f"        {label} for {app_name} (Enter to skip): ")
                answers[key] = value = value.strip()
            if not value:
                print(f"skipped {app_name}: {label}")
                # Host login covers Claude on a development instance, so it is
                # optional here rather than something still to do.
                if not (key == "claude" and settings.CLAUDE_USE_HOST_LOGIN):
                    missing.append((app_name, label))
                continue
            try:
                set_credential(admin, app.pk, key, value)
            except ValidationError as error:
                print(f"FAILED  {app_name}: {label}: {' '.join(error.messages)}")
                missing.append((app_name, label))
                continue
            print(f"set     {app_name}: {label}")
    return missing


def sync_all(admin, built):
    from platform_core.connectors import credential
    from platform_core.connectors import sync as sync_connector

    for app in built.values():
        for connector in Connector.objects.filter(application=app, enabled=True):
            if not credential(app, connector.kind):
                print(f"skipped {app.name}: {connector.name}, no credential")
                continue
            try:
                count = sync_connector(admin, connector.pk, app.pk)
            except Exception as error:  # noqa: BLE001 - reported, and the rest still run
                print(f"FAILED  {app.name}: {connector.name}: {error}")
                continue
            print(f"synced  {app.name}: {connector.name}, {count} record(s) changed")


def drive(built):
    """Run the intake, graph and code lanes here until they have nothing left.

    Every claim in those lanes is a guarded update, so a server running the same
    lanes at the same time takes different work rather than the same work twice.
    """
    from platform_core.document_worker import code_graph_steps, graph_steps, intake_steps

    ids = [app.pk for app in built.values()]
    steps = (*intake_steps(), *graph_steps(), *code_graph_steps())
    deadline = time.monotonic() + WAIT_SECONDS
    reported, said_at = None, 0.0
    while time.monotonic() < deadline:
        worked = False
        for step in steps:
            try:
                worked = bool(step()) or worked
            except Exception as error:  # noqa: BLE001 - one bad document must not stop the rest
                print(f"note    {step.__name__}: {error}")
                worked = True
        waiting = Document.objects.filter(
            application_id__in=ids, status__in=Document.IN_FLIGHT
        ).count()
        # Every ten seconds at most: one line per document was fifty-six lines.
        if waiting != reported and (not waiting or time.monotonic() - said_at >= 10):
            print(f"        {waiting} document(s) still converting")
            reported, said_at = waiting, time.monotonic()
        if not worked and not waiting:
            return True
        if not worked:
            time.sleep(2)
    print("Still busy after the wait. The server's worker will finish it; run this")
    print("again afterwards to publish the graph.")
    return False


def publish(admin, built):
    from platform_core.graphs import publish_revision, published_revision

    for app in built.values():
        current = published_revision(app.pk)
        if current is not None:
            print(f"found   {app.name}: graph version {current.number} published")
            continue
        latest = GraphRevision.objects.filter(application=app).order_by("-number").first()
        if latest is None:
            print(f"waiting {app.name}: no graph version has been generated yet")
            continue
        try:
            publish_revision(admin, app.pk, latest.number)
        except ValidationError as error:
            print(f"FAILED  {app.name}: {' '.join(error.messages)}")
            continue
        print(f"published {app.name}: graph version {latest.number}")


def report(built):
    from platform_core.readiness import all_steps

    for app in built.values():
        print(f"{app.name}  /applications/{app.pk}/")
        for step in all_steps(app):
            mark = "ok " if step.ok else ("-- " if not step.blocking else "XX ")
            print(f"  {mark} {step.label}: {step.detail}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--no-prompt", action="store_true", help="never ask for a credential")
    parser.add_argument(
        "--no-wait", action="store_true", help="stop after syncing; leave conversion to the server"
    )
    options = parser.parse_args(argv)

    admin = User.objects.filter(is_platform_admin=True, is_active=True).first()
    if admin is None:
        print("No platform administrator exists yet. Run this first, and choose")
        print("a password when it asks:")
        print()
        print("    .venv/Scripts/python.exe manage.py bootstrap_admin")
        return 1
    print(f"administrator: {admin.get_username()}")

    section("Workspace")
    org, made = Organization.objects.get_or_create(name="ACME")
    print(f"{'created' if made else 'found  '} organization ACME")
    OrganizationMember.objects.get_or_create(
        organization=org, user=admin, defaults={"is_admin": True}
    )
    portfolio, _ = Portfolio.objects.get_or_create(name="Integrated Care", organization=org)
    product, _ = Product.objects.get_or_create(name="Care Coordination", portfolio=portfolio)
    built = {
        name: build(admin, product, name, purpose, ai_purpose)
        for name, purpose, ai_purpose in APPLICATIONS
    }

    section("Sources")
    sources(admin, built)
    section("Connectors")
    connectors(admin, built)
    section("Credentials")
    missing = credentials(admin, built, prompt=not options.no_prompt)
    section("Import")
    sync_all(admin, built)
    if not options.no_wait:
        section("Conversion, graph and code index (no model is called)")
        drive(built)
        section("Publish")
        publish(admin, built)
    section("Checklist")
    report(built)

    if missing:
        print()
        print("Still to set, on each application's Settings > Credentials, then run")
        print("this again to import:")
        for app_name, label in missing:
            print(f"  {app_name}: {label}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
