"""Build the CarePath demonstration workspace in one run - and only this:

 0. Settings in config/local.toml: self-approval, demo reset, Code Graph local
    folders (this checkout's documentation/demo), writing to them and running their
    tests, and the shared connector-credential folder if none is set
 1. Organization ACME
 2. Portfolio Integrated Care
 3. Product Care Coordination
 4. Engineering application CarePathDev
 5. Operations application CarePathOps
 6. Users: acmeadmin (owner of ACME and of both applications) and the platform
    administrator siteadmin@db.com, both with password demo123456789
 7. The connectors and their configuration
 8. Where the Jira and ServiceNow credential files go, and their names
 9. Knowledge from the local folder documentation/demo/docs, built into a graph -
    with the model set for Graph generation in AI settings, then published
10. Code from the local folder documentation/demo/carepath, built into a code graph
11. Jira tickets imported into CarePathDev
12. ServiceNow tickets imported into CarePathOps

Run by `start-all.cmd -Demo`, or directly:

    .venv/Scripts/python.exe scripts/rebuild_demo.py

Idempotent: a second run reports what already exists and creates nothing new.
A password is set only when its user is created, never reset. Production must
declare demonstration = true and prompts for a strong initial password; the
published password below is only for development. Nothing here deletes anything.
Building with AI is paid, so it is asked before anything is loaded: document
typing after each conversion, the automatic AI enrichment of each new version,
and the one knowledge-graph generation per application step 9 asks for (once;
a re-run does not repeat it). At a console the build says who would pay - the
application's mounted Claude credential, or in development this machine's own
Claude Code login - and asks. `--ai` answers yes, `--no-ai` no, and with no
console and neither flag it builds without AI: an unattended run never spends
by default. Without AI the demo applications' AI tasks are left off, said in the
output, and a later run with `--ai` switches them back on.
"""

import argparse
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

from platform_core.connector_kinds import KINDS  # noqa: E402
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
DOCS_FOLDER = (
    Path("/opt/digitalbrain/demo-seed/docs") if settings.PRODUCTION
    else ROOT / "documentation" / "demo" / "docs"
)
CODE_FOLDER = (
    Path("/var/lib/digitalbrain/demo/carepath") if settings.PRODUCTION
    else ROOT / "documentation" / "demo" / "carepath"
)

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


#: The shared connector-credential folder the demo expects, when none is set.
CONNECTOR_FOLDER = "C:/DigitalBrain/secrets"


def wanted_settings(current):
    """(config key, TOML value, settings name, Python value) the demo needs.

    The folder roots are worked out from where this checkout is, so a clone in
    any directory reads its own documentation/demo. Roots already configured are
    kept and the demo folder is added beside them.
    """
    demo_root = CODE_FOLDER.parent.resolve()
    roots = [str(root) for root in current.get("code_graph_local_roots", [])]
    if not any(demo_root.is_relative_to(Path(root).resolve()) for root in roots):
        roots.append(demo_root.as_posix())
    wanted = [
        # The person who starts a run may approve its plan.
        ("allow_self_approval", "true", "ALLOW_SELF_APPROVAL", True),
        # The platform administrator may empty ACME between demos.
        ("allow_demo_reset", "true", "ALLOW_DEMO_RESET", True),
        # Code Graph may read documentation/demo/carepath ("From a folder on this server").
        ("code_graph_local_roots", json.dumps(roots), "CODE_GRAPH_LOCAL_ROOTS", roots),
        # Code Factory may write an approved change into that folder.
        ("code_factory_local_write", "true", "CODE_FACTORY_LOCAL_WRITE", True),
        # An approver may run the folder's own tests.
        ("code_factory_local_tests", "true", "CODE_FACTORY_LOCAL_TESTS", True),
    ]
    if not current.get("connector_secret_directory"):
        wanted.append(
            (
                "connector_secret_directory",
                json.dumps(CONNECTOR_FOLDER),
                "CONNECTOR_SECRET_DIRECTORY",
                CONNECTOR_FOLDER,
            )
        )
    return wanted


def demo_settings():
    """Write the demo's settings into the configuration file, line by line.

    Rewritten rather than round-tripped through TOML, as init_local.py does,
    so every other line and comment survives. They are applied to this run as
    well, so the steps below can use them; the server reads the file when it
    starts, which `start-all.cmd -Demo` does right after this script.
    """
    import tomllib

    if settings.PRODUCTION:
        # The entrypoint owns production configuration. Never append keys after
        # its [database]/[redis] tables or rewrite it beneath another container.
        required = ("DEMONSTRATION", "ALLOW_DEMO_RESET", "CODE_FACTORY_LOCAL_WRITE",
                    "CODE_FACTORY_LOCAL_TESTS", "SERVICEOPS_TEST_AUTOMATION")
        if not all(getattr(settings, name, False) for name in required):
            raise ValueError("Deploy with DIGITAL_BRAIN_DEMONSTRATION=1 before building the demo.")
        if not CODE_FOLDER.is_dir() or not DOCS_FOLDER.is_dir():
            raise ValueError("The demo image and persistent CarePath workspace are required.")
        print("found   deployed demonstration configuration (left unchanged)")
        return

    path = Path(os.environ.get("DIGITAL_BRAIN_CONFIG", ROOT / "config" / "local.toml"))
    current = tomllib.loads(path.read_text(encoding="utf-8"))
    wanted = wanted_settings(current)
    # A start without Docker runs a copy init_local.py writes from local.toml on
    # every start, so the settings go into local.toml as well or the next start
    # forgets them.
    paths = [path] + ([path.with_name("local.toml")] if path.name == "local.nodocker.toml" else [])
    for target in paths:
        lines = target.read_text(encoding="utf-8").splitlines()
        for key, value, name, applied in wanted:
            found = [i for i, line in enumerate(lines) if line.partition("=")[0].strip() == key]
            if found and lines[found[0]].partition("=")[2].strip() == value:
                if target == path:
                    print(f"found   {key} = {value}")
            else:
                if found:
                    lines[found[0]] = f"{key} = {value}"
                else:
                    # Before the first table: after the last one TOML would read
                    # the key as part of [originals] or [redis].
                    at = next((i for i, line in enumerate(lines) if line.startswith("[")),
                              len(lines))
                    lines.insert(at, f"{key} = {value}")
                if target == path:
                    print(f"set     {key} = {value}")
            setattr(settings, name, applied)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")


def step(number, title):
    print()
    print(f"== {number}. {title}")


def who_pays(app):
    """Whose account a Claude call for `app` would spend, or "" when none can be made."""
    from platform_core.agent_runtime.credentials import host_login_enabled
    from platform_core.secrets import source_of

    if source_of(app, "claude"):
        return f"{app.name}'s mounted Claude credential"
    if host_login_enabled():
        return "this machine's own Claude Code login (claude_use_host_login)"
    return ""


def wants_ai(choice, apps):
    """Whether this build may call the model: the flag, else the person at the console.

    No console and no flag is no: the build that spent on a sandbox's own login
    was a non-interactive run nobody was asked about.
    """
    payers = sorted({payer for payer in (who_pays(app) for app in apps) if payer})
    if not payers:
        print("note    no Claude credential and no host login: nothing can be charged")
        return True
    if choice is not None:
        return choice
    if not sys.stdin.isatty():
        print("note    building without AI: nobody is at a console to agree to the charges.")
        print("        Run with --ai to build with the model.")
        return False
    print("This build calls claude-sonnet-5 to type documents after conversion and to")
    print("enrich and generate each application's knowledge graph. It is charged to:")
    for payer in payers:
        print(f"  - {payer}")
    answer = input("Build with AI? [y/N] ").strip().lower()
    return answer in {"y", "yes"}


def without_ai(app):
    """Switch `app`'s AI tasks off for a build that was not agreed to spend.

    Through the same switches an owner uses in AI settings, so what the build
    declined is visible there and nothing in the worker is bypassed. Asked before
    step 9, the first step that can spend: conversion types each document, and a
    new version is enriched automatically.
    """
    from platform_core.models import AIConfiguration

    switched = AIConfiguration.objects.filter(application=app, enabled=True).update(
        enabled=False
    )
    if switched:
        print(f"set     {app.name}: AI tasks off for a build without AI "
              "(run with --ai to switch them back on)")


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
    # Every kind, as onboarding's connector question writes them: a kind with no
    # row reads as enabled, so naming only three left AWX, Rundeck, Azure
    # Automation and an automation list switched on, and onboarding asked for
    # credentials nobody meant to set.
    for kind in KINDS:
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
    # Every task on, as a new application starts: an application made before
    # that, or one somebody switched a task off on, is brought back to it.
    # Embeddings need an OpenAI credential, so without one they stay off and
    # this says where to set it.
    from platform_core.ai import has_embeddings_credential

    for config in AIConfiguration.objects.filter(application=app, enabled=False):
        if config.purpose == "embeddings" and not has_embeddings_credential(app):
            print(f"note    {name}: embeddings stay off - set an OpenAI key on its "
                  "Credentials screen and run this again")
            continue
        config.enabled = True
        config.save(update_fields=["enabled"])
        print(f"set     {name}: AI task {config.purpose} on")
    return app


def site_admin(password=None):
    """siteadmin@db.com as platform administrator, created once and never reset.

    Never elevates an account that already exists: bootstrap_admin refuses the
    same, and a demo build must not be a way round it.
    """
    from platform_core.models import AuditEvent

    user = User.objects.filter(username=SITE_ADMIN).first()
    if user is None:
        if settings.PRODUCTION and not password:
            raise ValueError("A password is required to create a VPS demo administrator.")
        user = User(username=SITE_ADMIN, is_platform_admin=True)
        user.set_password(password or DEMO_PASSWORD)
        user.save()
        AuditEvent.objects.create(
            actor=user, action="platform.admin_bootstrapped", resource_id=str(user.pk)
        )
        print(f"created platform administrator {SITE_ADMIN}")
    elif user.is_platform_admin:
        print(f"found   platform administrator {SITE_ADMIN} (password left as it is)")
    else:
        print(f"skipped {SITE_ADMIN} exists but is not a platform administrator; left as it is")
    if settings.PRODUCTION:
        return
    env = ROOT / ".env"
    if not env.exists():
        env.write_text(f"SITE_ADMIN_USER_ID={SITE_ADMIN}\n", encoding="utf-8")
        print(f"created .env naming {SITE_ADMIN} as the site administrator")


def demo_user(password=None):
    user = User.objects.filter(username=DEMO_USER).first()
    if user is not None:
        print(f"found   user {DEMO_USER} (password left as it is)")
        return user
    user = User(username=DEMO_USER)
    if settings.PRODUCTION and not password:
        raise ValueError("A password is required to create a VPS demo account.")
    # The demo's published password, set directly rather than through the
    # platform's password rules.
    user.set_password(password or DEMO_PASSWORD)
    user.save()
    print(f"created user {DEMO_USER}")
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


def drive(steps, finished, deadline):
    """Run worker steps here until `finished()` and nothing is left to do."""
    while time.monotonic() < deadline:
        worked = False
        for work in steps:
            try:
                worked = bool(work()) or worked
            except Exception as error:  # noqa: BLE001 - one bad document must not stop the rest
                print(f"note    {work.__name__}: {error}")
                worked = True
        if not worked and finished():
            return True
        if not worked:
            time.sleep(2)
    return False


def request_ai_graphs(owner, built):
    """Ask for one model-enriched graph per application, as Generate does.

    The same request the Generate form makes, through the same function, so the
    platform's rule holds: the background worker never calls a model on its own,
    and this is somebody - acmeadmin - asking for it. Asked once: an application
    that already has an enriched version is not charged again on a re-run.
    """
    from platform_core.graphs import claim_generation
    from platform_core.models import AIConfiguration

    requested = []
    for app in built.values():
        done = GraphRevision.objects.filter(application=app).exclude(model="").first()
        if done is not None:
            print(f"found   {app.name}: graph version {done.number} built with {done.model}")
            continue
        if not Document.objects.filter(application=app, status="ready").exists():
            print(f"skipped {app.name}: no converted documents to build from")
            continue
        config = AIConfiguration.objects.filter(
            application=app, purpose="graph_generation", enabled=True
        ).first()
        if config is None:
            print(f"skipped {app.name}: Graph generation is off in AI settings")
            continue
        claimed, refusal = claim_generation(owner, app, config.provider, config.model)
        if not claimed:
            print(f"skipped {app.name}: {refusal}")
            continue
        print(f"requested {app.name}: graph generation with {config.model} (charges apply)")
        requested.append(app)
    return requested


def build_graphs(built, ai=True):
    """Conversion and the code index, the graph with the model, then publish."""
    from platform_core.document_worker import code_graph_steps, graph_steps, intake_steps
    from platform_core.graphs import publish_revision, published_revision
    from platform_core.models import KnowledgeGraph

    ids = [app.pk for app in built.values()]
    deadline = time.monotonic() + WAIT_SECONDS

    def converted():
        return not Document.objects.filter(
            application_id__in=ids, status__in=Document.IN_FLIGHT
        ).exists()

    if not drive((*intake_steps(), *graph_steps(), *code_graph_steps()), converted, deadline):
        print("still busy; the server's worker will finish it. Run this again to publish.")
        return

    owner = User.objects.get(username=DEMO_USER)
    requested = request_ai_graphs(owner, built) if ai else []

    def enriched():
        return not KnowledgeGraph.objects.filter(
            application__in=requested, status__in=["queued", "building"]
        ).exists()

    if requested and not drive(graph_steps(), enriched, deadline):
        print("the model is still working; the server's worker will finish it. Run this")
        print("again afterwards to publish.")
        return
    for app in requested:
        made = GraphRevision.objects.filter(application=app).exclude(model="").first()
        if made is None:
            reason = KnowledgeGraph.objects.get(application=app).failure_reason
            print(f"FAILED  {app.name}: graph generation with the model. {reason}")

    for app in built.values():
        latest = GraphRevision.objects.filter(application=app).order_by("-number").first()
        if latest is None:
            print(f"waiting {app.name}: no graph version yet")
            continue
        current = published_revision(app.pk)
        # Left alone when someone published it, unless it is a structural version
        # and the model-enriched one this build asked for is newer.
        if current is not None and (current.number == latest.number or current.model):
            print(f"found   {app.name}: graph version {current.number} published")
            continue
        try:
            # Through the release check like any publish. The reason is used only
            # if a soft check fails, and says where it came from.
            publish_revision(
                owner, app.pk, latest.number, reason="Published by scripts/rebuild_demo.py"
            )
        except ValidationError as error:
            print(f"FAILED  {app.name}: {' '.join(error.messages)}")
            continue
        built_with = f" (built with {latest.model})" if latest.model else " (structural)"
        print(f"published {app.name}: graph version {latest.number}{built_with}")


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
    parser = argparse.ArgumentParser(description="Build the CarePath demonstration.")
    spending = parser.add_mutually_exclusive_group()
    spending.add_argument(
        "--ai", dest="ai", action="store_const", const=True, help="Build with the model."
    )
    spending.add_argument(
        "--no-ai", dest="ai", action="store_const", const=False, help="Build without it."
    )
    # An explicit list, never sys.argv by default: a test calling main() would
    # otherwise hand argparse the test runner's own arguments.
    arguments = parser.parse_args(argv if argv is not None else [])
    if settings.PRODUCTION and not settings.DEMONSTRATION:
        print("Refusing to build the demo on a production instance.")
        return 1

    step(0, "Settings")
    demo_settings()
    password = None
    new_accounts = User.objects.filter(username__in=[DEMO_USER, SITE_ADMIN]).count() < 2
    if settings.PRODUCTION and new_accounts:
        from getpass import getpass

        from django.contrib.auth.password_validation import validate_password

        # Interactive, hidden input: never an environment variable or a known
        # public password. Existing accounts are never changed or elevated.
        password = getpass("Initial password for new demo accounts: ")
        validate_password(password, User(username=DEMO_USER))
        if getpass("Confirm initial password: ") != password:
            raise ValueError("Passwords did not match; no demo data was created.")
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
    owner = demo_user(password)
    OrganizationMember.objects.update_or_create(
        organization=org, user=owner, defaults={"is_admin": True}
    )
    built = {}
    for number, (name, purpose, ai_purpose) in zip((4, 5), APPLICATIONS, strict=True):
        step(number, f"{purpose.capitalize()} application")
        built[name] = application(owner, product, name, purpose, ai_purpose)
    ai = wants_ai(arguments.ai, built.values())
    if not ai:
        for app in built.values():
            without_ai(app)
    step(6, "Users")
    print(f"{DEMO_USER} is owner (administrator) of ACME and owns CarePathDev and CarePathOps")
    site_admin(password)

    step(7, "Connectors")
    load_connectors(owner, built)
    step(8, "Credential files")
    missing = credential_files(built)
    step(9, "Knowledge from documentation/demo/docs")
    load_docs(owner, built)
    step(10, "Code from documentation/demo/carepath")
    load_code(owner, built["CarePathDev"])
    step(11, "Jira tickets into CarePathDev")
    import_tickets(owner, built["CarePathDev"], missing)
    step(12, "ServiceNow tickets into CarePathOps")
    import_tickets(owner, built["CarePathOps"], missing)

    print()
    print("== Building the knowledge graph" + (" (with the model)" if ai else " (structure only)")
          + " and the code graph")
    build_graphs(built, ai=ai)
    print()
    print(f"Sign in as {DEMO_USER}. Missing credential files: put them where step 8 says")
    print("and run this again; it imports what it could not before." if missing else "Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
