import io
import json
import logging
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from platform_core.models import (
    Application,
    ApplicationGrant,
    AuditEvent,
    Organization,
    OrganizationMember,
    Portfolio,
    Product,
    User,
)
from platform_core.observability import SafeJsonFormatter


@override_settings(
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class OperationsTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("siteadmin", is_platform_admin=True)
        self.owner = User.objects.create_user("orgadmin")
        self.member = User.objects.create_user("member")
        self.org = Organization.objects.create(name="Operations")
        OrganizationMember.objects.create(organization=self.org, user=self.owner, is_admin=True)
        OrganizationMember.objects.create(organization=self.org, user=self.member)
        portfolio = Portfolio.objects.create(name="Portfolio", organization=self.org)
        self.product = Product.objects.create(name="Product", portfolio=portfolio)
        self.app = Application.objects.create(name="Application", product=self.product)
        ApplicationGrant.objects.create(application=self.app, user=self.owner, role="owner")
        self.client.force_login(self.admin, backend="django.contrib.auth.backends.ModelBackend")

    def test_platform_user_management_and_first_password_change(self):
        response = self.client.post(
            reverse("users"),
            {
                "username": "newuser",
                "password1": "Fixture-Initial-Password-3849!",
                "password2": "Fixture-Initial-Password-3849!",
            },
        )
        self.assertRedirects(response, reverse("users"))
        user = User.objects.get(username="newuser")
        self.assertTrue(user.must_change_password)
        self.assertFalse(user.is_platform_admin)
        self.client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")
        self.assertRedirects(self.client.get("/"), reverse("password"))
        response = self.client.post(
            reverse("password"),
            {
                "old_password": "Fixture-Initial-Password-3849!",
                "new_password1": "Fixture-Changed-Password-9372!",
                "new_password2": "Fixture-Changed-Password-9372!",
            },
        )
        self.assertRedirects(response, "/")
        user.refresh_from_db()
        self.assertFalse(user.must_change_password)

    def test_disabling_user_revokes_session(self):
        self.client.post(reverse("user-status", args=[self.member.pk]), {"state": "disable"})
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_active)
        self.client.force_login(self.member, backend="django.contrib.auth.backends.ModelBackend")
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_last_owner_and_platform_admin_cannot_be_disabled(self):
        response = self.client.post(
            reverse("user-status", args=[self.owner.pk]), {"state": "disable"}
        )
        self.assertRedirects(response, reverse("users"))
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.is_active)
        self.assertEqual(
            self.client.post(
                reverse("user-status", args=[self.admin.pk]), {"state": "disable"}
            ).status_code,
            403,
        )

    def test_organization_lifecycle(self):
        url = reverse("organization-status", args=[self.org.pk])
        self.client.post(url, {"state": "disable"})
        self.org.refresh_from_db()
        self.assertFalse(self.org.active)
        self.client.post(url, {"state": "enable"})
        self.org.refresh_from_db()
        self.assertTrue(self.org.active)
        self.assertTrue(AuditEvent.objects.filter(action="organization.disabled").exists())

    def test_application_lifecycle_and_privilege_enforcement(self):
        url = reverse("application-status", args=[self.app.pk])
        self.assertEqual(self.client.post(url, {"state": "disable"}).status_code, 404)
        self.client.force_login(self.owner, backend="django.contrib.auth.backends.ModelBackend")
        self.client.post(url, {"state": "disable"})
        self.app.refresh_from_db()
        self.assertFalse(self.app.active)
        self.client.post(url, {"state": "enable"})
        self.app.refresh_from_db()
        self.assertTrue(self.app.active)

    def test_create_application_explicit_owner_and_approval(self):
        self.client.force_login(self.owner, backend="django.contrib.auth.backends.ModelBackend")
        response = self.client.post(
            reverse("application-new", args=[self.product.pk]),
            {"name": "New app", "owner": self.member.pk, "owner_can_approve": "on"},
        )
        self.assertEqual(response.status_code, 302)
        created = Application.objects.get(name="New app")
        grant = ApplicationGrant.objects.get(application=created)
        self.assertEqual(grant.user, self.member)
        self.assertTrue(grant.can_approve)
        self.assertEqual(grant.role, "owner")
        self.assertEqual(
            self.client.get(reverse("application", args=[created.pk])).status_code, 404
        )

    def test_request_id_and_log_never_include_secret_query(self):
        with self.assertLogs("digitalbrain.requests", level="INFO") as captured:
            response = self.client.get(
                "/?credential=SHOULD_NOT_APPEAR", HTTP_X_REQUEST_ID="attacker-value"
            )
        self.assertEqual(len(response["X-Request-ID"]), 36)
        formatter = SafeJsonFormatter()
        output = "\n".join(formatter.format(record) for record in captured.records)
        self.assertNotIn("SHOULD_NOT_APPEAR", output)
        self.assertNotIn("attacker-value", output)
        self.assertEqual(json.loads(output)["status"], 200)

    def test_oversized_request_rejected_before_view(self):
        response = self.client.post(
            reverse("branding"),
            data=b"x",
            content_type="image/png",
            CONTENT_LENGTH=24 * 1024 * 1024,
        )
        self.assertEqual(response.status_code, 413)
        self.assertIn("X-Request-ID", response)

    def test_json_logger_does_not_emit_exception_message(self):
        try:
            raise ValueError("secret-token")
        except ValueError:
            import sys

            record = logging.LogRecord(
                "test", logging.ERROR, "", 1, "secret-token", (), sys.exc_info()
            )
        output = SafeJsonFormatter().format(record)
        self.assertNotIn("secret-token", output)
        self.assertEqual(json.loads(output)["exception_type"], "ValueError")

    @override_settings(SITE_ADMIN_USER_ID="bootstrap-user")
    def test_bootstrap_refuses_existing_admin(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command("bootstrap_admin", stdout=io.StringIO())

    def test_bootstrap_works_once_without_superuser_access(self):
        self.admin.is_platform_admin = False
        self.admin.save()
        with (
            override_settings(SITE_ADMIN_USER_ID="initial-admin"),
            patch(
                "platform_core.management.commands.bootstrap_admin.getpass",
                return_value="Long-Unique-Test-Passphrase-6732!",
            ),
        ):
            call_command("bootstrap_admin", stdout=io.StringIO())
        initial = User.objects.get(username="initial-admin")
        self.assertTrue(initial.is_platform_admin)
        self.assertFalse(initial.is_superuser)
        self.assertFalse(initial.is_staff)

    def test_account_list_is_paginated(self):
        User.objects.bulk_create([User(username=f"person{i}") for i in range(40)])
        page = self.client.get(reverse("users")).context["accounts"]
        self.assertEqual(len(page), 30)
        self.assertTrue(page.has_next())

    def test_sole_organization_admin_cannot_be_disabled_without_app_ownership(self):
        ApplicationGrant.objects.filter(user=self.owner).delete()
        self.client.post(reverse("user-status", args=[self.owner.pk]), {"state": "disable"})
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.is_active)

    def test_audit_request_id_matches_response(self):
        response = self.client.post(
            reverse("features"), {"feature": "usage_reports", "state": "off"}
        )
        event = AuditEvent.objects.get(action="feature.disabled")
        self.assertEqual(event.request_id, response["X-Request-ID"])
