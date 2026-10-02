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
    # A new machine: nothing configured for local folders. Step 0 has to
    # enable them for step 10 to register the code folder in the same run.
    CODE_GRAPH_LOCAL_ROOTS=[],
    CODE_FACTORY_LOCAL_WRITE=False,
    CODE_FACTORY_LOCAL_TESTS=False,
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
            'mode = "development"\nhosts = ["localhost"]\n'
            f'secret_directory = "{Path(scratch.name).as_posix()}/secrets"\n'
            "# keep me\nallow_demo_reset = false\n",
            encoding="utf-8",
        )
        self.config = config
        environment = patch.dict("os.environ", {"DIGITAL_BRAIN_CONFIG": str(config)})
        environment.start()
        self.addCleanup(environment.stop)
        self.demo = builder()
        for stub in (
            # .env is written beside ROOT; never into the real checkout.
            patch.object(self.demo, "ROOT", Path(scratch.name)),
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

    def test_step_zero_writes_every_demo_setting_and_the_config_still_loads(self):
        from digitalbrain.configuration import load_config

        self.build()
        lines = self.config.read_text(encoding="utf-8").splitlines()
        demo_root = (ROOT / "demo-artifacts").resolve().as_posix()
        self.assertIn(f'code_graph_local_roots = ["{demo_root}"]', lines)
        self.assertIn("code_factory_local_write = true", lines)
        self.assertIn("code_factory_local_tests = true", lines)
        self.assertIn('connector_secret_directory = "C:/DigitalBrain/secrets"', lines)
        with patch.dict("os.environ", {"DIGITAL_BRAIN_CONFIG": str(self.config)}):
            loaded = load_config()
        self.assertEqual(loaded["code_graph_local_roots"], [demo_root])

    def test_self_approval_and_demo_reset_are_switched_on_in_the_config(self):
        self.build()
        lines = self.config.read_text(encoding="utf-8").splitlines()
        self.assertIn("allow_self_approval = true", lines)
        self.assertIn("allow_demo_reset = true", lines)
        self.assertNotIn("allow_demo_reset = false", lines)
        self.assertIn("# keep me", lines)
        self.build()
        self.assertEqual(self.config.read_text(encoding="utf-8").count("allow_demo_reset"), 1)

    def test_siteadmin_is_the_platform_administrator_with_the_demo_password(self):
        self.build()
        admin = User.objects.get(username="siteadmin@db.com")
        self.assertTrue(admin.is_platform_admin)
        self.assertTrue(admin.check_password("demo123456789"))
        env = (self.config.parent / ".env").read_text(encoding="utf-8")
        self.assertEqual(env, "SITE_ADMIN_USER_ID=siteadmin@db.com\n")

    def test_an_existing_siteadmin_is_never_elevated_or_reset(self):
        User.objects.create_user("siteadmin@db.com", password="their-own-password")
        self.build()
        admin = User.objects.get(username="siteadmin@db.com")
        self.assertFalse(admin.is_platform_admin)
        self.assertTrue(admin.check_password("their-own-password"))

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

    def built(self):
        self.build()
        return {app.name: app for app in Application.objects.all()}

    def test_the_knowledge_graph_is_requested_with_the_configured_model(self):
        from platform_core.models import Document, KnowledgeGraph

        built = self.built()
        Document.objects.filter(application=built["CarePathDev"]).update(status="ready")
        owner = User.objects.get(username="acmeadmin")
        requested = self.demo.request_ai_graphs(owner, built)
        self.assertEqual([app.name for app in requested], ["CarePathDev"])
        graph = KnowledgeGraph.objects.get(application=built["CarePathDev"])
        self.assertEqual(
            (graph.status, graph.requested_provider, graph.requested_model, graph.requested_by),
            ("queued", "claude", "claude-sonnet-5", owner),
        )

    def test_an_enriched_graph_is_never_paid_for_twice(self):
        from platform_core.models import Document, GraphRevision

        built = self.built()
        app = built["CarePathDev"]
        Document.objects.filter(application=app).update(status="ready")
        GraphRevision.objects.create(
            application=app, number=1, fingerprint="f", provider="claude", model="claude-sonnet-5"
        )
        owner = User.objects.get(username="acmeadmin")
        self.assertEqual(self.demo.request_ai_graphs(owner, {app.name: app}), [])

    def test_no_model_is_asked_for_when_graph_generation_is_off(self):
        from platform_core.models import AIConfiguration, Document

        built = self.built()
        app = built["CarePathDev"]
        Document.objects.filter(application=app).update(status="ready")
        AIConfiguration.objects.filter(application=app, purpose="graph_generation").update(
            enabled=False
        )
        owner = User.objects.get(username="acmeadmin")
        self.assertEqual(self.demo.request_ai_graphs(owner, {app.name: app}), [])

    def test_production_never_gets_a_known_password(self):
        with override_settings(PRODUCTION=True):
            self.assertEqual(self.demo.main(), 1)
        self.assertFalse(User.objects.filter(username="acmeadmin").exists())
