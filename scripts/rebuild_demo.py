"""Rebuild the CarePath demonstration workspace from nothing.

Everything here is derived from demo-artifacts/demo-kit/demo-setup.md, which is
the runbook a person would follow by hand. This does the parts that need no
credential, so the only manual steps left are the ones that involve a secret:
the administrator's password, and the Jira, GitHub-write and Claude credentials.

Idempotent. Run it twice and the second run reports what already existed rather
than creating a second ACME. Nothing here deletes anything.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "digitalbrain.settings")

import django  # noqa: E402

django.setup()

from platform_core.models import (  # noqa: E402
    AIConfiguration,
    Application,
    ApplicationFeature,
    ApplicationGrant,
    Organization,
    OrganizationMember,
    Portfolio,
    Product,
    User,
)

DOCS = "https://github.com/ajitk15/carepathdocs/tree/main"
CODE = "ajitk15/carepath"

#: Ticked on the create form in the runbook. chat_api is deliberately absent:
#: it is the one API surface that spends the application's model budget, so it
#: stays opt-in and gets an explicit disabled row rather than a missing one.
ENABLED = (
    "knowledge",
    "code_factory",
    "code_graph",
    "connectors",
    "service_ops",
    "chat",
    "usage_reports",
)
DISABLED = ("chat_api",)


def main():
    admin = User.objects.filter(is_platform_admin=True, is_active=True).first()
    if admin is None:
        print("No platform administrator exists yet. Run this first, and choose")
        print("a password when it asks:")
        print()
        print("    .venv/Scripts/python.exe manage.py bootstrap_admin")
        return 1
    print(f"administrator: {admin.get_username()}")

    org, made = Organization.objects.get_or_create(name="ACME")
    print(f"{'created' if made else 'found  '} organization ACME")

    _, made = OrganizationMember.objects.get_or_create(
        organization=org, user=admin, defaults={"is_admin": True}
    )
    print(f"{'created' if made else 'found  '} membership for {admin.get_username()}")

    portfolio, made = Portfolio.objects.get_or_create(name="Integrated Care", organization=org)
    print(f"{'created' if made else 'found  '} portfolio Integrated Care")

    product, made = Product.objects.get_or_create(name="Care Coordination", portfolio=portfolio)
    print(f"{'created' if made else 'found  '} product Care Coordination")

    app, made = Application.objects.get_or_create(name="CarePath", product=product)
    print(f"{'created' if made else 'found  '} application CarePath  ({app.pk})")

    # Owner, and explicitly able to approve. On this instance allow_self_approval
    # is on, which is what lets one operator run the pipeline end to end; a real
    # deployment grants approval to a second person instead.
    grant, made = ApplicationGrant.objects.get_or_create(
        application=app, user=admin, defaults={"role": "owner", "can_approve": True}
    )
    if not made and not grant.can_approve:
        grant.can_approve = True
        grant.save(update_fields=["can_approve"])
    print(f"{'created' if made else 'found  '} owner grant with approval rights")

    for key in ENABLED:
        ApplicationFeature.objects.update_or_create(
            application=app, key=key, defaults={"enabled": True}
        )
    for key in DISABLED:
        ApplicationFeature.objects.update_or_create(
            application=app, key=key, defaults={"enabled": False}
        )
    print(f"features: {len(ENABLED)} enabled, {len(DISABLED)} left off ({', '.join(DISABLED)})")

    # Plan drafting is the one a Code Factory run refuses without. The rates are
    # what the receipts are priced at; they do not change what is called.
    config, made = AIConfiguration.objects.get_or_create(
        application=app,
        purpose="plan_drafting",
        defaults={
            "provider": "claude",
            "model": "claude-sonnet-5",
            "enabled": True,
            "input_rate": 3,
            "output_rate": 15,
            "configured_by": admin,
        },
    )
    print(f"{'created' if made else 'found  '} AI configuration {config.provider} {config.model}")

    # ---- the two imports, both public, both anonymous ----
    from platform_core.code_graph_ingest import register
    from platform_core.link_sources import submit
    from platform_core.models import CodeRepository, KnowledgeSource

    if KnowledgeSource.objects.filter(application=app, url=DOCS).exists():
        print("found   knowledge source carepathdocs")
    else:
        notes = []
        created = submit(admin, app.pk, DOCS, notes)
        print(f"created knowledge source carepathdocs: {len(created)} document(s) queued")
        for note in notes:
            print(f"        {note}")

    if CodeRepository.objects.filter(application=app, external_id=CODE.lower()).exists():
        print("found   code repository ajitk15/carepath")
    else:
        repo = register(admin, app.pk, CODE)
        print(f"created code repository {repo.name}, queued for indexing")

    print()
    print("Left to do, because each one needs a secret I will not handle:")
    print(f"  Credentials screen: Claude, GitHub (write), Jira   (application {app.pk})")
    print("  Knowledge: generate a graph and publish it once the documents convert")
    print()
    print("The worker converts documents and indexes the repository in the")
    print("background; start the server and watch the Knowledge and Code Graph")
    print("screens settle.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
