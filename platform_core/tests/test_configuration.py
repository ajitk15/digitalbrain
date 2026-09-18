import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from digitalbrain.configuration import admin_identity, load_config, read_secret


class ConfigurationTests(SimpleTestCase):
    def test_dotenv_accepts_only_bootstrap_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch("digitalbrain.configuration.ROOT", root),
                patch.dict("os.environ", {}, clear=True),
            ):
                (root / ".env").write_text("SITE_ADMIN_USER_ID=admin@example.com\n")
                self.assertEqual(admin_identity(), "admin@example.com")
                (root / ".env").write_text("SITE_ADMIN_USER_ID=admin\nAPI_KEY=forbidden\n")
                with self.assertRaises(ImproperlyConfigured):
                    admin_identity()

    def test_missing_secret_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ImproperlyConfigured):
                read_secret(directory, "missing")

    def test_empty_and_overpermissive_secret_rejected(self):
        import os

        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / "django_secret_key"
            secret.touch(mode=0o600)
            with self.assertRaises(ImproperlyConfigured):
                read_secret(directory, "django_secret_key")
            if os.name != "nt":
                secret.write_text("test-key")
                secret.chmod(0o644)
                with self.assertRaises(ImproperlyConfigured):
                    read_secret(directory, "django_secret_key")

    def test_config_rejects_password_and_wildcard_host(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            with patch.dict("os.environ", {"DIGITAL_BRAIN_CONFIG": str(config)}):
                config.write_text('mode="production"\nhosts=["*"]\n')
                with self.assertRaises(ImproperlyConfigured):
                    load_config()
                config.write_text(
                    'mode="production"\nhosts=["host"]\n[database]\npassword="forbidden"\n'
                )
                with self.assertRaises(ImproperlyConfigured):
                    load_config()


class HostLoginConfigurationTests(SimpleTestCase):
    """claude_use_host_login is a development convenience and must stay one."""

    BASE = 'hosts = ["localhost"]\nsecret_directory = "/tmp/secrets"\n'

    def load(self, body):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(body, encoding="utf-8")
            with patch.dict("os.environ", {"DIGITAL_BRAIN_CONFIG": str(path)}):
                return load_config()

    def test_it_is_accepted_in_development(self):
        config = self.load(f'mode = "development"\n{self.BASE}claude_use_host_login = true\n')
        self.assertTrue(config["claude_use_host_login"])

    def test_it_defaults_to_absent(self):
        config = self.load(f'mode = "development"\n{self.BASE}')
        self.assertNotIn("claude_use_host_login", config)

    def test_production_refuses_it_outright(self):
        """Refused, not ignored: a production deployment must not silently share one identity."""
        with self.assertRaises(ImproperlyConfigured) as raised:
            self.load(f'mode = "production"\n{self.BASE}claude_use_host_login = true\n')
        self.assertIn("cannot be set in production", str(raised.exception))

    def test_it_must_be_a_boolean(self):
        with self.assertRaises(ImproperlyConfigured):
            self.load(f'mode = "development"\n{self.BASE}claude_use_host_login = "yes"\n')


class ImportMaxFilesTests(SimpleTestCase):
    """The ceiling on one directory import, and the bounds on the bound itself."""

    BASE = 'hosts = ["localhost"]\nsecret_directory = "/tmp/secrets"\n'

    def load(self, body):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(body, encoding="utf-8")
            with patch.dict("os.environ", {"DIGITAL_BRAIN_CONFIG": str(path)}):
                return load_config()

    def test_it_is_optional(self):
        config = self.load(f'mode = "development"\n{self.BASE}')
        self.assertNotIn("import_max_files", config)

    def test_an_operator_may_raise_it(self):
        config = self.load(f'mode = "development"\n{self.BASE}import_max_files = 250\n')
        self.assertEqual(config["import_max_files"], 250)

    def test_zero_is_refused(self):
        """A ceiling of nothing makes every import fail for a reason nobody would guess."""
        with self.assertRaises(ImproperlyConfigured):
            self.load(f'mode = "development"\n{self.BASE}import_max_files = 0\n')

    def test_an_enormous_value_is_refused(self):
        """Removing the bound is not configuring it."""
        with self.assertRaises(ImproperlyConfigured):
            self.load(f'mode = "development"\n{self.BASE}import_max_files = 100000\n')

    def test_it_must_be_a_whole_number(self):
        for value in ('"100"', "12.5", "true"):
            with self.subTest(value=value):
                with self.assertRaises(ImproperlyConfigured):
                    self.load(f'mode = "development"\n{self.BASE}import_max_files = {value}\n')


class SelfApprovalConfigurationTests(SimpleTestCase):
    """allow_self_approval is a development convenience and must stay one.

    Built like claude_use_host_login and for the same reason: a deployment that
    believes two people review every change must not find out from its audit
    log that one did.
    """

    BASE = 'hosts = ["localhost"]\nsecret_directory = "/tmp/secrets"\n'

    load = HostLoginConfigurationTests.load

    def test_it_is_accepted_in_development(self):
        config = self.load(f'mode = "development"\n{self.BASE}allow_self_approval = true\n')
        self.assertTrue(config["allow_self_approval"])

    def test_it_defaults_to_absent(self):
        config = self.load(f'mode = "development"\n{self.BASE}')
        self.assertNotIn("allow_self_approval", config)

    def test_production_refuses_it_rather_than_ignoring_it(self):
        with self.assertRaises(ImproperlyConfigured) as refusal:
            self.load(f'mode = "production"\n{self.BASE}allow_self_approval = true\n')
        self.assertIn("cannot be set in production", str(refusal.exception))

    def test_it_must_be_a_boolean(self):
        with self.assertRaises(ImproperlyConfigured):
            self.load(f'mode = "development"\n{self.BASE}allow_self_approval = "yes"\n')


class DemoResetConfigurationTests(SimpleTestCase):
    """allow_demo_reset is for demonstration instances and must stay one.

    Nothing else in this platform deletes an application, and a button that
    empties a workspace has no business on a deployment holding work somebody
    depends on.
    """

    BASE = 'hosts = ["localhost"]\nsecret_directory = "/tmp/secrets"\n'

    load = HostLoginConfigurationTests.load

    def test_it_is_accepted_in_development(self):
        config = self.load(f'mode = "development"\n{self.BASE}allow_demo_reset = true\n')
        self.assertTrue(config["allow_demo_reset"])

    def test_it_defaults_to_absent(self):
        config = self.load(f'mode = "development"\n{self.BASE}')
        self.assertNotIn("allow_demo_reset", config)

    def test_production_refuses_it_rather_than_ignoring_it(self):
        with self.assertRaises(ImproperlyConfigured) as refusal:
            self.load(f'mode = "production"\n{self.BASE}allow_demo_reset = true\n')
        self.assertIn("cannot be set in production", str(refusal.exception))

    def test_it_must_be_a_boolean(self):
        with self.assertRaises(ImproperlyConfigured):
            self.load(f'mode = "development"\n{self.BASE}allow_demo_reset = "yes"\n')
