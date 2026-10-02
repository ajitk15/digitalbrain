"""The one-click demo build, as a new machine runs it.

The imports from Jira and ServiceNow and the graph build are stubbed, so this
checks what the builder creates, not what those systems hold.
"""

import importlib.util
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, override_settings

from platform_core.models import (
    Application,
    ApplicationGrant,
    CodeRepository,
    KnowledgeSource,
    Organization,
    OrganizationMember,
    User,
)

ROOT = Path(__file__).resolve().parents[2]


def builder():
    spec = importlib.util.spec_from_file_location("rebuild_demo", ROOT / "scripts/rebuild_demo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@override_settings(
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    DOCUMENT_AUTO_CONVERT=False,
    CODE_GRAPH_LOCAL_ROOTS=[str(ROOT / "demo-artifacts")],
)
class RebuildDemoTests(TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        storage = override_settings(BASE_DIR=Path(scratch.name))
        storage.enable()
        self.addCleanup(storage.disable)
        config = Path(scratch.name) / "local.toml"
        config.write_text(
            'mode = "development"\n# keep me\nallow_demo_reset = false\n', encoding="utf-8"
        )
        self.config = config
        environment = patch.dict("os.environ", {"DIGITAL_BRAIN_CONFIG": str(config)})
        environment.start()
        self.addCleanup(environment.stop)
        self.demo = builder()
        for stub in (
            patch.object(self.demo, "build_graphs"),
            patch("platform_core.connectors.sync", return_value=0),
        ):
            stub.start()
            self.addCleanup(stub.stop)

    def build(self):
        self.assertEqual(self.demo.main(), 0)

    def test_a_new_machine_gets_acmeadmin_acme_and_both_applications(self):
        self.build()
        owner = User.objects.get(username="acmeadmin")
        self.assertTrue(owner.check_password("demo123456789"))
        self.assertFalse(owner.is_platform_admin)
        org = Organization.objects.get(name="ACME")
        self.assertTrue(OrganizationMember.objects.get(organization=org, user=owner).is_admin)
        for name in ("CarePathDev", "CarePathOps"):
            grant = ApplicationGrant.objects.get(application__name=name, user=owner)
            self.assertEqual((grant.role, grant.can_approve), ("owner", True))
            source = KnowledgeSource.objects.get(application__name=name)
            self.assertEqual((source.provider, source.name), ("folder", "docs"))
            self.assertEqual(source.documents.exclude(status="deleted").count(), 27)
        repository = CodeRepository.objects.get(application__name="CarePathDev")
        self.assertEqual((repository.provider, repository.name), ("local", "carepath"))
        self.assertEqual(
            sorted(Application.objects.values_list("product__portfolio__name", "product__name")),
            [("Integrated Care", "Care Coordination")] * 2,
        )

    def test_self_approval_and_demo_reset_are_switched_on_in_the_config(self):
        self.build()
        lines = self.config.read_text(encoding="utf-8").splitlines()
        self.assertIn("allow_self_approval = true", lines)
        self.assertIn("allow_demo_reset = true", lines)
        self.assertNotIn("allow_demo_reset = false", lines)
        self.assertIn("# keep me", lines)
        self.build()
        self.assertEqual(self.config.read_text(encoding="utf-8").count("allow_demo_reset"), 1)

    def test_running_it_again_creates_nothing_and_keeps_the_password(self):
        self.build()
        owner = User.objects.get(username="acmeadmin")
        owner.set_password("changed-by-the-owner")
        owner.save()
        self.build()
        self.assertEqual(Application.objects.count(), 2)
        self.assertEqual(Organization.objects.count(), 1)
        self.assertEqual(KnowledgeSource.objects.count(), 2)
        self.assertEqual(CodeRepository.objects.count(), 1)
        owner.refresh_from_db()
        self.assertTrue(owner.check_password("changed-by-the-owner"))

    def test_production_never_gets_a_known_password(self):
        with override_settings(PRODUCTION=True):
            self.assertEqual(self.demo.main(), 1)
        self.assertFalse(User.objects.filter(username="acmeadmin").exists())
