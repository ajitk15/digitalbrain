"""Whose audit events each administrator reads.

A platform administrator used to see only events with no organization, so a
disable, a deletion or an administrator change they made themselves - each
recorded with its organization - never reached their own audit log. These pin
that they now see every control-plane event, and still no application's.
"""

from django.test import TestCase, override_settings
from django.urls import reverse

from platform_core import hierarchy
from platform_core.models import (
    Application,
    AuditEvent,
    Organization,
    OrganizationMember,
    Portfolio,
    Product,
    User,
)
from platform_core.services import CONTROL_PLANE, audit


@override_settings(
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class AuditScopeTests(TestCase):
    def setUp(self):
        self.site = User.objects.create_user("siteadmin", is_platform_admin=True)
        self.alice = User.objects.create_user("alice")
        self.org = Organization.objects.create(name="Northstar")
        self.other = Organization.objects.create(name="Elsewhere")
        OrganizationMember.objects.create(organization=self.org, user=self.alice, is_admin=True)
        portfolio = Portfolio.objects.create(name="Commerce", organization=self.org)
        self.app = Application.objects.create(
            name="Refunds", product=Product.objects.create(name="Payments", portfolio=portfolio)
        )

    def actions(self, user):
        self.client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")
        response = self.client.get(reverse("audit"))
        self.assertEqual(response.status_code, 200)
        return {event.action for event in response.context["page"]}

    def test_a_platform_administrator_sees_their_own_organization_changes(self):
        self.client.force_login(self.site, backend="django.contrib.auth.backends.ModelBackend")
        self.client.post(
            reverse("organization-administrators", args=[self.org.pk]),
            {"administrators": [self.site.pk]},
        )
        hierarchy.set_active(self.site, self.app, False)
        hierarchy.set_active(self.site, self.other, False)
        seen = self.actions(self.site)
        self.assertLessEqual(
            {"organization.admin_added", "organization.admin_removed",
             "application.disabled", "organization.disabled"},
            seen,
        )

    def test_a_deleted_organization_still_shows_that_it_was_deleted(self):
        hierarchy.set_active(self.site, self.other, False)
        hierarchy.delete(self.site, self.other, self.other.name)
        response_actions = self.actions(self.site)
        self.assertIn("organization.deleted", response_actions)
        self.client.force_login(self.site, backend="django.contrib.auth.backends.ModelBackend")
        self.assertContains(self.client.get(reverse("audit")), "Elsewhere")

    def test_a_platform_administrator_sees_no_application_events(self):
        audit(self.alice, "knowledge.created", "entry", self.org)
        audit(self.alice, "application.identity_updated", self.app.pk, self.org)
        seen = self.actions(self.site)
        self.assertNotIn("knowledge.created", seen)
        self.assertNotIn("application.identity_updated", seen)

    def test_a_platform_administrator_sees_who_was_granted_access(self):
        audit(self.alice, "application.access_changed", self.alice.pk, self.org)
        audit(self.alice, "application.access_revoked", self.alice.pk, self.org)
        seen = self.actions(self.site)
        self.assertLessEqual({"application.access_changed", "application.access_revoked"}, seen)

    def test_administering_an_organization_as_well_shows_its_whole_stream(self):
        OrganizationMember.objects.create(organization=self.org, user=self.site, is_admin=True)
        audit(self.alice, "knowledge.created", "entry", self.org)
        audit(self.alice, "knowledge.created", "elsewhere", self.other)
        self.client.force_login(self.site, backend="django.contrib.auth.backends.ModelBackend")
        page = self.client.get(reverse("audit")).context["page"]
        found = {(event.action, event.resource_id) for event in page}
        self.assertIn(("knowledge.created", "entry"), found)
        self.assertNotIn(("knowledge.created", "elsewhere"), found)

    def test_an_organization_administrator_sees_no_other_organization(self):
        hierarchy.set_active(self.site, self.other, False)
        audit(self.site, "account.created", self.alice.pk)
        seen = self.actions(self.alice)
        self.assertNotIn("organization.disabled", seen)
        self.assertNotIn("account.created", seen)

    def test_every_hierarchy_action_is_control_plane(self):
        def covered(action):
            return any(
                action.startswith(entry) if entry.endswith(".") else action == entry
                for entry in CONTROL_PLANE
            )

        for level in hierarchy.LEVELS:
            for verb in ("enabled", "disabled", "deleted", "files_left"):
                with self.subTest(level=level, verb=verb):
                    self.assertTrue(covered(f"{level}.{verb}"))
        self.assertFalse(AuditEvent.objects.exists())
