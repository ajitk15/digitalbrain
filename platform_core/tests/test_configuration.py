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
