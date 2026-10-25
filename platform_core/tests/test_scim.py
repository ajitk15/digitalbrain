"""Provisioning from the directory over SCIM: creates and deactivates, never grants.

The requests are the shapes Microsoft Entra ID's provisioning service sends -
its connection test, a filter by userName, a create, `Replace` with `"False"`,
an `emails[type eq "work"].value` path, a value with no path, attributes the
platform does not keep - so a change here is checked against the client that
will really call it.
"""

import json
import tempfile
from pathlib import Path

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from platform_core import scim
from platform_core.models import (
    Application,
    ApplicationGrant,
    AuditEvent,
    Organization,
    OrganizationMember,
    Portfolio,
    Product,
    ProvisionedUser,
    User,
)
from platform_core.policy import applications_for

from . import test_sso
from .test_sso import CLIENT, TENANT

TOKEN = "t" * 48
SETTINGS = {
    "STORAGES": {
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}
    },
    "PASSWORD_HASHERS": ["django.contrib.auth.hashers.MD5PasswordHasher"],
    "SCIM_PROVISIONING": True,
}


@override_settings(**SETTINGS)
class ProvisioningTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.secrets = Path(directory.name)
        token = self.secrets / scim.SECRET_NAME
        token.write_text(TOKEN)
        # A mounted secret must be owner-only on POSIX, or read_secret refuses it.
        token.chmod(0o600)
        mounted = override_settings(SECRET_DIRECTORY=directory.name)
        mounted.enable()
        self.addCleanup(mounted.disable)
        self.admin = User.objects.create_user("siteadmin@example.com", is_platform_admin=True)
        self.dana = User.objects.create_user(
            "dana", email="dana@contoso.example", password="not-a-real-password"
        )

    # ── requests as Entra sends them ─────────────────────────────────────────

    def call(self, method, path, body=None, token=TOKEN):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        data = json.dumps(body) if body is not None else None
        return getattr(self.client, method)(
            f"/api/scim/v2{path}", data=data, content_type="application/scim+json", **headers
        )

    def create(self, user_name="kim@contoso.example", **extra):
        body = {
            "schemas": [scim.USER_SCHEMA],
            "externalId": "kim",
            "userName": user_name,
            "active": True,
            "displayName": "Kim Lee",
            "name": {"givenName": "Kim", "familyName": "Lee", "formatted": "Kim Lee"},
            "emails": [{"primary": True, "type": "work", "value": user_name}],
            "title": "Engineer",
            "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User": {"department": "Ops"},
            **extra,
        }
        return self.call("post", "/Users", body)

    def patch(self, pk, *operations):
        return self.call(
            "patch",
            f"/Users/{pk}",
            {"schemas": [scim.PATCH_SCHEMA], "Operations": list(operations)},
        )

    def find(self, user_name):
        return self.call("get", f'/Users?filter=userName eq "{user_name}"').json()

    # ── who may call it ──────────────────────────────────────────────────────

    def test_switched_off_every_address_is_not_found(self):
        with override_settings(SCIM_PROVISIONING=False):
            self.assertEqual(self.call("get", "/Users").status_code, 404)
            self.assertEqual(self.create().status_code, 404)
        self.assertFalse(User.objects.filter(username="kim@contoso.example").exists())

    def test_only_the_mounted_token_is_accepted(self):
        for token in ("", "wrong", TOKEN[:-1]):
            with self.subTest(token=token):
                response = self.call("post", "/Users", {"userName": "kim"}, token=token)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["schemas"], [scim.ERROR_SCHEMA])
        # A signed-in browser is not a provisioning client.
        self.client.force_login(self.admin, backend="django.contrib.auth.backends.ModelBackend")
        self.assertEqual(self.call("post", "/Users", {"userName": "kim"}, token="").status_code,
                         401)
        self.assertFalse(User.objects.filter(username="kim").exists())

    def test_a_missing_or_short_token_refuses_rather_than_opens(self):
        (self.secrets / scim.SECRET_NAME).write_text("short")
        self.assertEqual(self.call("get", "/Users", token="short").status_code, 503)
        (self.secrets / scim.SECRET_NAME).unlink()
        self.assertEqual(self.call("get", "/Users", token="").status_code, 503)

    # ── creating ─────────────────────────────────────────────────────────────

    def test_entras_connection_test_finds_nothing_and_writes_nothing(self):
        response = self.call(
            "get", '/Users?filter=userName eq "a0e0a8f1-5b2b-4f5c-8b1e-3f0a1c2d3e4f"'
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], scim.CONTENT_TYPE)
        self.assertEqual(response.json()["totalResults"], 0)
        self.assertEqual(response.json()["schemas"], [scim.LIST_SCHEMA])
        self.assertFalse(AuditEvent.objects.exists())

    def test_a_created_account_can_do_nothing_until_granted(self):
        response = self.create()
        self.assertEqual(response.status_code, 201)
        body = response.json()
        user = User.objects.get(pk=body["id"])
        self.assertTrue(response["Location"].endswith(f"/api/scim/v2/Users/{user.pk}"))
        self.assertEqual(
            (user.username, user.email, user.first_name, user.last_name, user.is_active),
            ("kim@contoso.example", "kim@contoso.example", "Kim", "Lee", True),
        )
        self.assertFalse(user.has_usable_password())
        self.assertFalse(user.is_platform_admin)
        self.assertFalse(OrganizationMember.objects.filter(user=user).exists())
        self.assertFalse(ApplicationGrant.objects.filter(user=user).exists())
        self.assertEqual(list(applications_for(user)), [])
        self.assertEqual(body["externalId"], "kim")
        event = AuditEvent.objects.get(action="account.provisioned")
        self.assertEqual(event.actor.username, scim.PROVISIONER)
        self.assertFalse(event.actor.is_active)
        self.assertFalse(event.actor.has_usable_password())
        # Found again by the name it was made with.
        self.assertEqual(self.find("KIM@contoso.example")["Resources"][0]["id"], str(user.pk))

    def test_a_second_create_with_the_same_name_conflicts(self):
        self.create()
        response = self.create()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["scimType"], "uniqueness")

    # ── platform administrators are an operator's ────────────────────────────

    def test_a_platform_administrator_is_never_matched_created_over_or_changed(self):
        self.assertEqual(self.find(self.admin.username)["totalResults"], 0)
        self.assertEqual(self.create(user_name=self.admin.username).status_code, 409)
        self.assertEqual(self.patch(self.admin.pk, {"op": "Replace", "path": "active",
                                                    "value": "False"}).status_code, 404)
        self.assertEqual(self.call("delete", f"/Users/{self.admin.pk}").status_code, 404)
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)
        self.assertFalse(ProvisionedUser.objects.filter(user=self.admin).exists())

    # ── matching an account someone made by hand ─────────────────────────────

    def test_an_existing_account_is_matched_by_email_once_and_audited(self):
        found = self.find("dana@contoso.example")
        self.assertEqual(found["Resources"][0]["id"], str(self.dana.pk))
        self.assertTrue(ProvisionedUser.objects.filter(user=self.dana).exists())
        self.assertEqual(AuditEvent.objects.filter(action="account.directory_linked").count(), 1)
        self.find("dana@contoso.example")
        self.assertEqual(AuditEvent.objects.filter(action="account.directory_linked").count(), 1)
        # A password it already had is left alone.
        self.dana.refresh_from_db()
        self.assertTrue(self.dana.has_usable_password())

    def test_an_unmatched_account_cannot_be_read_or_changed(self):
        self.assertEqual(self.call("get", f"/Users/{self.dana.pk}").status_code, 404)
        self.assertEqual(self.patch(self.dana.pk, {"op": "replace", "path": "active",
                                                   "value": False}).status_code, 404)
        self.assertEqual(self.call("get", "/Users/not-an-id").status_code, 404)
        self.dana.refresh_from_db()
        self.assertTrue(self.dana.is_active)

    # ── changing ─────────────────────────────────────────────────────────────

    def test_entras_patch_dialect_updates_what_is_kept_and_ignores_the_rest(self):
        pk = self.create().json()["id"]
        response = self.patch(
            pk,
            {"op": "Replace", "path": 'emails[type eq "work"].value', "value": "kim.lee@x.example"},
            {"op": "Replace", "path": "name.givenName", "value": "Kimberly"},
            {"op": "Add", "value": {"name.familyName": "Lee-Park", "externalId": "kim2"}},
            {"op": "Replace", "path": "title", "value": "Lead"},
            {"op": "Replace", "path": "urn:ietf:params:scim:schemas:extension:enterprise:2.0:"
                                      "User:department", "value": "Eng"},
        )
        self.assertEqual(response.status_code, 200)
        user = User.objects.get(pk=pk)
        self.assertEqual(
            (user.email, user.first_name, user.last_name),
            ("kim.lee@x.example", "Kimberly", "Lee-Park"),
        )
        self.assertEqual(response.json()["externalId"], "kim2")
        event = AuditEvent.objects.get(action="account.updated")
        self.assertEqual(
            event.details, {"fields": ["email", "external_id", "first_name", "last_name"]}
        )
        # Field names only, never what they were set to.
        self.assertNotIn("Kimberly", json.dumps(event.details))

    def test_a_rename_onto_someone_elses_name_conflicts(self):
        pk = self.create().json()["id"]
        response = self.patch(pk, {"op": "Replace", "path": "userName", "value": "DANA"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(User.objects.get(pk=pk).username, "kim@contoso.example")

    def test_an_unknown_operation_is_refused(self):
        pk = self.create().json()["id"]
        response = self.patch(pk, {"op": "move", "path": "active", "value": "False"})
        self.assertEqual(response.status_code, 400)

    # ── leaving ──────────────────────────────────────────────────────────────

    def test_unassigning_deactivates_signs_out_and_says_what_is_left_without_an_owner(self):
        org = Organization.objects.create(name="Contoso")
        app = Application.objects.create(
            name="A",
            product=Product.objects.create(
                name="P", portfolio=Portfolio.objects.create(name="F", organization=org)
            ),
        )
        OrganizationMember.objects.create(organization=org, user=self.dana, is_admin=True)
        ApplicationGrant.objects.create(application=app, user=self.dana, role="owner")
        pk = self.find("dana")["Resources"][0]["id"]
        self.client.force_login(self.dana, backend="django.contrib.auth.backends.ModelBackend")
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)

        response = self.patch(pk, {"op": "Replace", "path": "active", "value": "False"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["active"])
        self.dana.refresh_from_db()
        self.assertFalse(self.dana.is_active)
        # Their session no longer signs them in.
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 302)
        event = AuditEvent.objects.get(action="account.deprovisioned")
        self.assertEqual(event.details, {"organizations": [str(org.pk)],
                                         "applications": [str(app.pk)]})

        self.patch(pk, {"op": "Replace", "path": "active", "value": "True"})
        self.dana.refresh_from_db()
        self.assertTrue(self.dana.is_active)
        self.assertTrue(AuditEvent.objects.filter(action="account.reprovisioned").exists())

    def test_deleting_in_the_directory_deactivates_and_keeps_the_account(self):
        pk = self.create().json()["id"]
        response = self.call("delete", f"/Users/{pk}")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        user = User.objects.get(pk=pk)
        self.assertFalse(user.is_active)
        self.assertFalse(ProvisionedUser.objects.filter(user=user).exists())
        self.assertTrue(AuditEvent.objects.filter(action="account.directory_unlinked").exists())
        self.assertEqual(self.call("get", f"/Users/{pk}").status_code, 404)

    # ── what it will not do ──────────────────────────────────────────────────

    def test_groups_are_not_accepted(self):
        response = self.call("post", "/Groups", {"displayName": "Admins", "members": []})
        self.assertEqual(response.status_code, 404)
        self.assertIn("Groups are not accepted", response.json()["detail"])
        self.assertEqual(self.call("post", "/Groups", {}, token="").status_code, 401)

    def test_only_a_simple_filter_is_understood(self):
        response = self.call("get", '/Users?filter=userName sw "a"')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["scimType"], "invalidFilter")

    def test_a_person_using_the_provisioning_name_stops_it_rather_than_being_blamed(self):
        User.objects.create_user(scim.PROVISIONER, password="not-a-real-password")
        self.assertEqual(self.create().status_code, 500)
        self.assertFalse(User.objects.filter(username="kim@contoso.example").exists())


class ProvisioningConfigurationTests(SimpleTestCase):
    load = test_sso.SignInConfigurationTests.load

    def test_it_must_be_a_boolean(self):
        from django.core.exceptions import ImproperlyConfigured

        with self.assertRaises(ImproperlyConfigured):
            self.load(
                f'entra_tenant = "{TENANT}"\nentra_client_id = "{CLIENT}"\n'
                'scim_provisioning = "yes"\n'
            )

    def test_it_needs_microsoft_sign_in(self):
        from django.core.exceptions import ImproperlyConfigured

        with self.assertRaises(ImproperlyConfigured):
            self.load("scim_provisioning = true\n")
        config = self.load(
            f'entra_tenant = "{TENANT}"\nentra_client_id = "{CLIENT}"\nscim_provisioning = true\n'
        )
        self.assertTrue(config["scim_provisioning"])


class DeploymentTests(SimpleTestCase):
    """The container renders sign-in and provisioning from its environment."""

    def test_the_entrypoint_renders_both_only_when_named_and_warns_without_secrets(self):
        entrypoint = (
            Path(__file__).resolve().parents[2] / "deploy/entrypoint.sh"
        ).read_text(encoding="utf-8")
        self.assertIn('if [ -n "${DIGITAL_BRAIN_ENTRA_TENANT:-}" ]; then', entrypoint)
        self.assertIn('if [ "${DIGITAL_BRAIN_SCIM:-0}" = "1" ]; then', entrypoint)
        # Provisioning is rendered only inside the Entra block: load_config
        # would refuse it alone, and the container would not start.
        tenant = entrypoint.index("DIGITAL_BRAIN_ENTRA_TENANT:-}")
        self.assertLess(tenant, entrypoint.index("echo 'scim_provisioning = true'"))
        for name in ("entra_client_secret", "scim_token"):
            self.assertIn(f"[ -f /run/secrets/{name} ] ||", entrypoint)
