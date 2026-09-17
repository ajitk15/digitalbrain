"""Credentials an application owner sets from the browser.

The deliberate change this file pins: a secret may now arrive in a request body.
Everything the original rule protected has to survive that, so these tests assert
the properties rather than the implementation - the value reaches a file and
nothing else, an operator's mount still wins, and one application cannot reach
another's credential.
"""

import os
import tempfile
from pathlib import Path

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from platform_core.models import AuditEvent, ManagedCredential
from platform_core.secrets import (
    application_secret,
    clear_credential,
    set_credential,
    source_of,
)

from . import test_documents

TOKEN = "ATATT-not-a-real-token-5f3c9"


class CredentialTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.mounted = tempfile.TemporaryDirectory()
        self.managed = tempfile.TemporaryDirectory()
        self.addCleanup(self.mounted.cleanup)
        self.addCleanup(self.managed.cleanup)
        self.directories = override_settings(
            SECRET_DIRECTORY=self.mounted.name, MANAGED_SECRET_DIRECTORY=self.managed.name
        )
        self.directories.enable()
        self.addCleanup(self.directories.disable)
        self.url = reverse("credentials", args=[self.app.pk])

    def mount(self, name, value):
        """What an operator's secret manager projects into the read-only mount."""
        path = Path(self.mounted.name) / name
        path.write_text(value, encoding="utf-8")
        if os.name != "nt":
            path.chmod(0o600)
        return path

    def save(self, key="jira", secret=TOKEN, app=None):
        return self.client.post(
            reverse("credentials", args=[(app or self.app).pk]),
            {"credential": key, "secret": secret},
        )

    def test_an_owner_sets_a_credential_and_the_platform_can_read_it(self):
        self.assertEqual(self.save().status_code, 302)
        self.assertEqual(application_secret(self.app, "jira"), TOKEN)
        self.assertEqual(source_of(self.app, "jira"), "managed")

    def test_the_value_is_written_to_a_file_the_service_account_alone_can_read(self):
        self.save()
        path = Path(self.managed.name) / f"jira_{self.app.pk}"
        self.assertTrue(path.is_file())
        self.assertEqual(path.read_text(encoding="utf-8"), TOKEN)
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_the_value_never_reaches_the_database(self):
        """The row records who and when. The secret is not in it, in any field."""
        self.save()
        record = ManagedCredential.objects.get(application=self.app, name="jira")
        self.assertNotIn(TOKEN, str(record.__dict__))
        self.assertEqual(record.updated_by, self.owner)

    def test_the_value_never_reaches_the_audit_log(self):
        self.save()
        event = AuditEvent.objects.filter(action="credential.set").latest("created_at")
        self.assertEqual(event.details, {"credential": "jira"})
        self.assertNotIn(TOKEN, str(event.details))

    def test_the_value_is_never_rendered_back_to_the_browser(self):
        self.save()
        page = self.client.get(self.url)
        self.assertNotContains(page, TOKEN)
        self.assertContains(page, "Set")

    def test_an_operator_mount_takes_precedence_over_the_browser(self):
        """A deployment projecting credentials from a secret manager is not
        overridden by something typed into a form."""
        self.save(secret="entered-in-the-browser")
        self.mount(f"jira_{self.app.pk}", "projected-by-the-operator")
        self.assertEqual(application_secret(self.app, "jira"), "projected-by-the-operator")
        self.assertEqual(source_of(self.app, "jira"), "operator")

    def test_the_screen_does_not_offer_to_replace_an_operator_mount(self):
        self.mount(f"jira_{self.app.pk}", "projected-by-the-operator")
        page = self.client.get(self.url)
        self.assertContains(page, "Managed by your operator")

    def test_clearing_removes_the_file_and_the_row(self):
        self.save()
        self.client.post(self.url, {"credential": "jira", "action": "clear"})
        self.assertFalse((Path(self.managed.name) / f"jira_{self.app.pk}").exists())
        self.assertFalse(ManagedCredential.objects.filter(application=self.app).exists())
        self.assertEqual(application_secret(self.app, "jira"), "")

    def test_clearing_never_removes_an_operator_mount(self):
        mounted = self.mount(f"jira_{self.app.pk}", "projected-by-the-operator")
        clear_credential(self.owner, self.app.pk, "jira")
        self.assertTrue(mounted.is_file())
        self.assertEqual(application_secret(self.app, "jira"), "projected-by-the-operator")

    def test_replacing_overwrites_rather_than_appending(self):
        self.save(secret="first")
        self.save(secret="second")
        self.assertEqual(application_secret(self.app, "jira"), "second")
        self.assertEqual(ManagedCredential.objects.filter(application=self.app).count(), 1)

    def test_one_application_cannot_read_another_credential(self):
        self.save()
        self.assertEqual(application_secret(self.other, "jira"), "")

    def test_a_viewer_cannot_set_or_see_credentials(self):
        self.client.force_login(self.viewer, backend="django.contrib.auth.backends.ModelBackend")
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.save().status_code, 403)
        self.assertFalse(ManagedCredential.objects.exists())

    def test_a_user_with_no_grant_is_told_the_application_does_not_exist(self):
        """Deny by default: a 404, not a 403, exactly as everywhere else."""
        stranger = test_documents.User.objects.create_user("stranger")
        self.client.force_login(stranger, backend="django.contrib.auth.backends.ModelBackend")
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_an_unknown_credential_is_refused(self):
        with self.assertRaises(ValidationError):
            set_credential(self.owner, self.app.pk, "../../etc/passwd", TOKEN)
        with self.assertRaises(ValidationError):
            set_credential(self.owner, self.app.pk, "database_password", TOKEN)
        self.assertEqual(len(list(Path(self.managed.name).iterdir())), 0)

    def test_an_empty_or_multiline_credential_is_refused(self):
        for value in ("", "   ", "line one\nline two"):
            with self.assertRaises(ValidationError):
                set_credential(self.owner, self.app.pk, "jira", value)
        with self.assertRaises(ValidationError):
            set_credential(self.owner, self.app.pk, "jira", "x" * 9000)
        self.assertEqual(application_secret(self.app, "jira"), "")

    def test_surrounding_whitespace_is_trimmed(self):
        """A value pasted with a trailing newline is the commonest way a
        credential arrives, and it must not become part of the secret."""
        self.save(secret=f"  {TOKEN}\t")
        self.assertEqual(application_secret(self.app, "jira"), TOKEN)

    @override_settings(MANAGED_SECRET_DIRECTORY="")
    def test_a_deployment_without_a_directory_offers_nothing_and_accepts_nothing(self):
        page = self.client.get(self.url)
        self.assertContains(page, "nowhere to store a credential")
        with self.assertRaises(ValidationError):
            set_credential(self.owner, self.app.pk, "jira", TOKEN)
        self.assertFalse(ManagedCredential.objects.exists())


class ConnectorUseTests(TestCase):
    """The credential an owner sets is the one a connector import actually uses."""

    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.managed = tempfile.TemporaryDirectory()
        self.addCleanup(self.managed.cleanup)
        self.directories = override_settings(MANAGED_SECRET_DIRECTORY=self.managed.name)
        self.directories.enable()
        self.addCleanup(self.directories.disable)

    def test_a_connector_reads_a_credential_set_in_the_browser(self):
        from platform_core.connectors import credential

        set_credential(self.owner, self.app.pk, "jira", TOKEN)
        self.assertEqual(credential(self.app, "jira"), TOKEN)

    def test_code_factory_delivery_reads_its_own_separate_credential(self):
        """github_write stays a different credential from github: setting the
        read token must not grant the ability to push."""
        from platform_core.code_factory import write_credential

        set_credential(self.owner, self.app.pk, "github", TOKEN)
        self.assertEqual(write_credential(self.app), "")
        set_credential(self.owner, self.app.pk, "github_write", "write-token")
        self.assertEqual(write_credential(self.app), "write-token")
