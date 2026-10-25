"""First-run setup: configuration repair and AI credential seeding.

These cover the two things that made a moved project look broken - a
config/local.toml still naming the old machine's secret directory, and a new
application with no credential and no AI configuration at all.
"""

import importlib.util
import tempfile
from pathlib import Path

from django.test import TestCase, override_settings
from django.urls import reverse

from platform_core.ai import (
    DEFAULT_CREDENTIAL_NAMES,
    DEFAULT_MODELS,
    SEEDED_DISABLED,
    configured_providers,
    seed_application_ai,
)
from platform_core.models import (
    AI_PURPOSES,
    AIConfiguration,
    Application,
    ApplicationGrant,
    Organization,
    OrganizationMember,
    Portfolio,
    Product,
    User,
)

ROOT = Path(__file__).resolve().parent.parent.parent


def load_script(name):
    """Import a scripts/*.py module by path; they are not on the import path."""
    specification = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


class ConfigurationRepairTests(TestCase):
    """scripts/init_local.py rewriting a stale absolute secret_directory."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = Path(self.directory.name) / "local.toml"

        self.repair = load_script("init_local").repair_config

    def test_a_stale_absolute_path_is_rewritten_and_other_keys_survive(self):
        self.config.write_text(
            'mode = "development"\n'
            'hosts = ["localhost"]\n'
            'secret_directory = "D:/OldMachine/digitalbrian/.runtime/secrets"\n'
            "# a comment the operator wrote\n"
            "claude_use_host_login = true\n",
            encoding="utf-8",
        )
        expected = "C:/New/.runtime/secrets"
        stale = self.repair(self.config, expected)
        self.assertEqual(stale, "D:/OldMachine/digitalbrian/.runtime/secrets")
        body = self.config.read_text(encoding="utf-8")
        self.assertIn(f'secret_directory = "{expected}"', body)
        self.assertNotIn("OldMachine", body)
        # The point of a line rewrite rather than a TOML round-trip.
        self.assertIn("claude_use_host_login = true", body)
        self.assertIn("# a comment the operator wrote", body)

    def test_a_correct_path_is_left_untouched(self):
        expected = "C:/Right/secrets"
        self.config.write_text(
            f'mode = "development"\nsecret_directory = "{expected}"\n', encoding="utf-8"
        )
        before = self.config.read_text(encoding="utf-8")
        self.assertIsNone(self.repair(self.config, expected))
        self.assertEqual(self.config.read_text(encoding="utf-8"), before)

    def test_a_missing_key_is_added_rather_than_silently_ignored(self):
        self.config.write_text('mode = "development"\nhosts = ["localhost"]\n', encoding="utf-8")
        self.assertIsNone(self.repair(self.config, "C:/Added/secrets"))
        self.assertIn(
            'secret_directory = "C:/Added/secrets"', self.config.read_text(encoding="utf-8")
        )


class DevelopmentDefaultsTests(TestCase):
    """scripts/init_local.py switching development features on, and starting without Docker."""

    SERVICES = (
        'mode = "development"\n'
        'hosts = ["localhost"]\n'
        'secret_directory = "C:/s"\n'
        "allow_demo_reset = false\n"
        'clamd = "127.0.0.1:3310"\n'
        "\n"
        "[originals]\n"
        'store = "s3"\n'
        'bucket = "b"\n'
        "\n"
        "[database]\n"
        'name = "digital_brain"\n'
        "port = 5442\n"
        "\n"
        "[redis]\n"
        'host = "127.0.0.1"\n'
    )

    def setUp(self):
        self.module = load_script("init_local")
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "project"
        self.config = Path(self.directory.name) / "local.toml"

    def read(self, path):
        import tomllib

        return tomllib.loads(path.read_text(encoding="utf-8"))

    def test_absent_keys_are_switched_on_above_the_first_table(self):
        self.config.write_text(self.SERVICES, encoding="utf-8")
        added = self.module.switch_on(self.config, self.root)
        current = self.read(self.config)
        for key in (
            "claude_use_host_login",
            "code_factory_local_write",
            "code_factory_local_tests",
            "code_graph_local_roots",
            "managed_secret_directory",
            "ocr",
        ):
            self.assertIn(key, added)
            self.assertIn(key, current, key)
        self.assertTrue(current["claude_use_host_login"])
        # Top-level keys, not swallowed by the tables after them.
        self.assertEqual(set(current["redis"]), {"host"})
        self.assertEqual(set(current["originals"]), {"store", "bucket"})

    def test_a_key_written_down_stays_as_written(self):
        self.config.write_text(self.SERVICES, encoding="utf-8")
        added = self.module.switch_on(self.config, self.root)
        self.assertNotIn("allow_demo_reset", added)
        self.assertFalse(self.read(self.config)["allow_demo_reset"])
        before = self.config.read_text(encoding="utf-8")
        self.assertEqual(self.module.switch_on(self.config, self.root), [])
        self.assertEqual(self.config.read_text(encoding="utf-8"), before)

    def test_code_graph_never_reads_a_drive_root(self):
        drive = Path(Path.cwd().anchor)
        self.assertEqual(self.module.local_roots(drive / "app"), [(drive / "app").as_posix()])
        self.assertEqual(
            self.module.local_roots(drive / "work" / "app"), [(drive / "work").as_posix()]
        )

    def test_the_file_without_docker_drops_only_the_services(self):
        self.config.write_text(self.SERVICES, encoding="utf-8")
        target = self.module.without_services(self.config)
        current = self.read(target)
        self.assertNotIn("database", current)
        self.assertNotIn("redis", current)
        self.assertNotIn("clamd", current)
        self.assertEqual(current["originals"]["bucket"], "b")
        self.assertFalse(current["allow_demo_reset"])
        # local.toml itself is untouched: Docker is used again when it is back.
        self.assertIn("database", self.read(self.config))

    def test_no_file_when_local_toml_names_no_service(self):
        stale = self.config.with_name("local.nodocker.toml")
        stale.write_text("left from before", encoding="utf-8")
        self.config.write_text('mode = "development"\nsecret_directory = "C:/s"\n',
                               encoding="utf-8")
        self.assertIsNone(self.module.without_services(self.config))
        self.assertFalse(stale.exists())

    def test_production_still_refuses_what_development_switches_on(self):
        import os
        from unittest import mock

        from django.core.exceptions import ImproperlyConfigured

        from digitalbrain.configuration import load_config

        for key, value, _ in self.module.development(self.root):
            if value != "true":
                continue
            if key == "ocr":
                continue
            self.config.write_text(
                f'mode = "production"\nsecret_directory = "C:/s"\n{key} = true\n',
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"DIGITAL_BRAIN_CONFIG": str(self.config)}):
                with self.assertRaisesMessage(ImproperlyConfigured, key):
                    load_config()


class HostLoginFlagTests(TestCase):
    """scripts/ai_setup.py editing config/local.toml in place."""

    def setUp(self):
        self.module = load_script("ai_setup")
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = Path(self.directory.name) / "local.toml"
        self.original = self.module.CONFIG
        self.module.CONFIG = self.config
        self.addCleanup(setattr, self.module, "CONFIG", self.original)

    def test_the_flag_is_added_when_absent(self):
        self.config.write_text('mode = "development"\nhosts = ["localhost"]\n', encoding="utf-8")
        self.module.set_host_login(True)
        self.assertIn("claude_use_host_login = true", self.config.read_text(encoding="utf-8"))

    def test_the_flag_is_replaced_not_duplicated(self):
        self.config.write_text(
            'mode = "development"\nhosts = ["x"]\nclaude_use_host_login = true\n', encoding="utf-8"
        )
        self.module.set_host_login(False)
        body = self.config.read_text(encoding="utf-8")
        self.assertEqual(body.count("claude_use_host_login"), 1)
        self.assertIn("claude_use_host_login = false", body)
        self.assertIn('hosts = ["x"]', body)

    def test_the_flag_is_added_above_the_tables(self):
        self.config.write_text(
            'mode = "development"\n\n# The development PostgreSQL.\n[database]\nport = 5442\n',
            encoding="utf-8",
        )
        self.module.set_host_login(True)
        import tomllib

        current = tomllib.loads(self.config.read_text(encoding="utf-8"))
        self.assertTrue(current["claude_use_host_login"])
        self.assertEqual(current["database"], {"port": 5442})

    def test_every_other_key_survives_the_rewrite(self):
        self.config.write_text(
            'mode = "development"\n'
            'hosts = ["localhost", "127.0.0.1"]\n'
            'secret_directory = "C:/keep/me"\n'
            'fetch_allow_hosts = ["internal.example"]\n',
            encoding="utf-8",
        )
        self.module.set_host_login(True)
        body = self.config.read_text(encoding="utf-8")
        for kept in ('secret_directory = "C:/keep/me"', 'fetch_allow_hosts = ["internal.example"]'):
            self.assertIn(kept, body)


class CredentialSeedingTests(TestCase):
    """An application created after setup should be able to answer immediately."""

    def setUp(self):
        self.secrets = tempfile.TemporaryDirectory()
        self.addCleanup(self.secrets.cleanup)
        self.admin = User.objects.create_user("orgadmin")
        self.owner = User.objects.create_user("appowner")
        self.org = Organization.objects.create(name="Seeding")
        OrganizationMember.objects.create(organization=self.org, user=self.admin, is_admin=True)
        OrganizationMember.objects.create(organization=self.org, user=self.owner)
        portfolio = Portfolio.objects.create(name="Portfolio", organization=self.org)
        self.product = Product.objects.create(name="Product", portfolio=portfolio)

    def write_default(self, provider, value):
        path = Path(self.secrets.name) / DEFAULT_CREDENTIAL_NAMES[provider]
        path.write_text(value, encoding="utf-8")
        return path

    def make_application(self):
        return Application.objects.create(name="Seeded", product=self.product)

    def test_nothing_is_seeded_when_setup_supplied_nothing(self):
        with override_settings(SECRET_DIRECTORY=self.secrets.name, CLAUDE_USE_HOST_LOGIN=False):
            app = self.make_application()
            self.assertIsNone(seed_application_ai(self.admin, app))
            self.assertFalse(AIConfiguration.objects.filter(application=app).exists())

    def test_the_default_credential_is_copied_to_a_per_application_file(self):
        self.write_default("claude", "sk-ant-api-EXAMPLE")
        with override_settings(SECRET_DIRECTORY=self.secrets.name, CLAUDE_USE_HOST_LOGIN=False):
            app = self.make_application()
            self.assertEqual(seed_application_ai(self.admin, app), "claude")
            mounted = Path(self.secrets.name) / f"claude_{app.pk}"
            self.assertTrue(mounted.is_file())
            self.assertEqual(mounted.read_text(encoding="utf-8"), "sk-ant-api-EXAMPLE")

    def test_a_credential_mounted_on_purpose_is_never_overwritten(self):
        self.write_default("claude", "sk-ant-api-DEFAULT")
        with override_settings(SECRET_DIRECTORY=self.secrets.name, CLAUDE_USE_HOST_LOGIN=False):
            app = self.make_application()
            mounted = Path(self.secrets.name) / f"claude_{app.pk}"
            mounted.write_text("sk-ant-api-DELIBERATE", encoding="utf-8")
            seed_application_ai(self.admin, app)
            self.assertEqual(mounted.read_text(encoding="utf-8"), "sk-ant-api-DELIBERATE")

    def test_every_purpose_is_configured_and_graph_generation_stays_off(self):
        self.write_default("claude", "sk-ant-api-EXAMPLE")
        with override_settings(SECRET_DIRECTORY=self.secrets.name, CLAUDE_USE_HOST_LOGIN=False):
            app = self.make_application()
            seed_application_ai(self.admin, app)
        rows = {row.purpose: row for row in AIConfiguration.objects.filter(application=app)}
        self.assertEqual(set(rows), {purpose for purpose, _ in AI_PURPOSES})
        # Search embeddings are OpenAI's whatever setup chose, and off with no
        # OpenAI credential to call them with.
        embeddings = rows.pop("embeddings")
        self.assertEqual((embeddings.provider, embeddings.enabled), ("openai", False))
        self.assertEqual(SEEDED_DISABLED, set())
        for purpose, row in rows.items():
            self.assertEqual(row.provider, "claude")
            self.assertEqual(row.model, DEFAULT_MODELS["claude"])
            self.assertEqual(row.enabled, purpose not in SEEDED_DISABLED)
        # Enabled only makes enrichment available; a paid run is still requested.
        self.assertTrue(rows["graph_generation"].enabled)

    def test_an_openai_template_switches_embeddings_on_and_is_copied_not_read(self):
        self.write_default("claude", "sk-ant-api-EXAMPLE")
        self.write_default("openai", "sk-openai-EXAMPLE")
        with override_settings(SECRET_DIRECTORY=self.secrets.name, CLAUDE_USE_HOST_LOGIN=False):
            app = self.make_application()
            seed_application_ai(self.admin, app)
            copied = Path(self.secrets.name) / f"openai_{app.pk}"
            self.assertEqual(copied.read_text(encoding="utf-8"), "sk-openai-EXAMPLE")
        rows = {row.purpose: row for row in AIConfiguration.objects.filter(application=app)}
        self.assertTrue(rows["embeddings"].enabled)
        self.assertTrue(all(row.enabled for row in rows.values()))

    def test_host_login_alone_seeds_a_configuration_with_no_file(self):
        with override_settings(SECRET_DIRECTORY=self.secrets.name, CLAUDE_USE_HOST_LOGIN=True):
            app = self.make_application()
            self.assertEqual(seed_application_ai(self.admin, app), "claude")
            self.assertFalse((Path(self.secrets.name) / f"claude_{app.pk}").exists())
        self.assertTrue(AIConfiguration.objects.filter(application=app, purpose="chat").exists())

    def test_seeding_is_idempotent(self):
        self.write_default("openai", "sk-EXAMPLE")
        with override_settings(SECRET_DIRECTORY=self.secrets.name, CLAUDE_USE_HOST_LOGIN=False):
            app = self.make_application()
            seed_application_ai(self.admin, app)
            seed_application_ai(self.admin, app)
        self.assertEqual(AIConfiguration.objects.filter(application=app).count(), len(AI_PURPOSES))

    def test_configured_providers_reports_only_what_setup_mounted(self):
        with override_settings(SECRET_DIRECTORY=self.secrets.name):
            self.assertEqual(configured_providers(), [])
            self.write_default("openai", "sk-EXAMPLE")
            self.assertEqual(configured_providers(), ["openai"])

    @override_settings(
        STORAGES={
            "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}
        },
        PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    )
    def test_creating_an_application_through_the_view_seeds_it(self):
        self.write_default("claude", "sk-ant-api-EXAMPLE")
        self.client.force_login(self.admin, backend="django.contrib.auth.backends.ModelBackend")
        with override_settings(SECRET_DIRECTORY=self.secrets.name, CLAUDE_USE_HOST_LOGIN=False):
            response = self.client.post(
                reverse("application-new", args=[self.product.pk]),
                {"name": "Through the view", "owner": self.owner.pk},
            )
        self.assertEqual(response.status_code, 302)
        app = Application.objects.get(name="Through the view")
        self.assertTrue(ApplicationGrant.objects.filter(application=app, role="owner").exists())
        self.assertTrue(
            AIConfiguration.objects.filter(application=app, purpose="chat", enabled=True).exists()
        )
        self.assertTrue((Path(self.secrets.name) / f"claude_{app.pk}").is_file())
