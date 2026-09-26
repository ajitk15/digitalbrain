"""Rebuild the CarePath demonstration workspace from nothing.

Everything here is derived from demo-artifacts/demo-kit/demo-setup.md, which is
the runbook a person would follow by hand. Two applications, one per purpose:

* CarePathDev - Engineering: Jira tickets, the CarePath repository, Code Factory.
* CarePathOps - Operations: ServiceNow incidents and changes, ServiceOps triage.

Both read the same lifecycle documents. This does the parts that need no
credential, so the only manual steps left are the ones that involve a secret:
the administrator's password, and the Jira, ServiceNow, GitHub-write and Claude
credentials. Connectors are left to onboarding, which asks for them.

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


def build(admin, product, name, purpose, ai_purpose):
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
    return app


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
    OrganizationMember.objects.get_or_create(
        organization=org, user=admin, defaults={"is_admin": True}
    )
    portfolio, _ = Portfolio.objects.get_or_create(name="Integrated Care", organization=org)
    product, _ = Product.objects.get_or_create(name="Care Coordination", portfolio=portfolio)

    from platform_core.code_graph_ingest import register
    from platform_core.link_sources import submit
    from platform_core.models import CodeRepository, KnowledgeSource

    built = {}
    for name, purpose, ai_purpose in APPLICATIONS:
        app = built[name] = build(admin, product, name, purpose, ai_purpose)
        if KnowledgeSource.objects.filter(application=app, url=DOCS).exists():
            print("found   knowledge source carepathdocs")
        else:
            notes = []
            created = submit(admin, app.pk, DOCS, notes)
            print(f"created knowledge source carepathdocs: {len(created)} document(s) queued")

    dev = built["CarePathDev"]
    if CodeRepository.objects.filter(application=dev, external_id=CODE.lower()).exists():
        print("found   code repository ajitk15/carepath")
    else:
        repo = register(admin, dev.pk, CODE)
        print(f"created code repository {repo.name}, queued for indexing")

    print()
    print("Left to do, because each one needs a secret I will not handle:")
    print(f"  CarePathDev credentials: Claude, Jira, GitHub (write)  ({dev.pk})")
    print(f"  CarePathOps credentials: Claude, ServiceNow            ({built['CarePathOps'].pk})")
    print("  Then follow each application's onboarding: its connectors, a")
    print("  graph generated and published once the documents convert, and an import.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
