"""Offboarding hands what someone held to a named successor, then disables them."""

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from platform_core import offboarding
from platform_core.models import (
    ApiToken,
    Application,
    ApplicationGrant,
    AuditEvent,
    Connector,
    KnowledgeGraph,
    Organization,
    OrganizationMember,
    Portfolio,
    Product,
    User,
)


@override_settings(
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class OffboardingTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("root", is_platform_admin=True)
        self.leaver = User.objects.create_user("leaver")
        self.heir = User.objects.create_user("heir")
        self.org = Organization.objects.create(name="Northstar")
        OrganizationMember.objects.create(organization=self.org, user=self.leaver, is_admin=True)
        OrganizationMember.objects.create(organization=self.org, user=self.heir)
        portfolio = Portfolio.objects.create(name="Commerce", organization=self.org)
        product = Product.objects.create(name="Payments", portfolio=portfolio)
        self.app = Application.objects.create(name="Refunds", product=product)
        ApplicationGrant.objects.create(
            application=self.app, user=self.leaver, role="owner", can_approve=True
        )
        self.connector = Connector.objects.create(
            application=self.app, kind="jira", name="Board", created_by=self.leaver
        )
        self.graph = KnowledgeGraph.objects.create(
            application=self.app, auto_refresh_by=self.leaver
        )

    def grant(self, user):
        return ApplicationGrant.objects.filter(application=self.app, user=user).first()

    def test_the_successor_takes_over_and_the_leaver_is_disabled(self):
        ApiToken.objects.create(
            application=self.app, user=self.leaver, name="ci", prefix="dbk_ci", digest="ci"
        )
        done = offboarding.offboard(self.admin, self.leaver.pk, "heir")
        self.assertEqual(done["applications"], 1)
        self.assertEqual(done["tokens"], 1)
        self.assertEqual(self.grant(self.heir).role, "owner")
        self.assertTrue(self.grant(self.heir).can_approve)
        self.assertTrue(
            OrganizationMember.objects.get(organization=self.org, user=self.heir).is_admin
        )
        self.connector.refresh_from_db()
        self.assertEqual(self.connector.created_by, self.heir)
        self.graph.refresh_from_db()
        self.assertIsNone(self.graph.auto_refresh_by)
        self.assertIn("leaver left", self.graph.auto_refresh_note)
        self.leaver.refresh_from_db()
        self.assertFalse(self.leaver.is_active)
        self.assertIsNotNone(self.grant(self.leaver))
        self.assertFalse(ApiToken.objects.filter(revoked_at__isnull=True).exists())
        self.assertTrue(AuditEvent.objects.filter(action="account.offboarded").exists())

    def test_a_successor_outside_the_organization_is_refused_and_nothing_moves(self):
        OrganizationMember.objects.filter(user=self.heir).delete()
        with self.assertRaisesMessage(ValidationError, "not a member of Northstar"):
            offboarding.offboard(self.admin, self.leaver.pk, "heir")
        self.leaver.refresh_from_db()
        self.assertTrue(self.leaver.is_active)
        self.assertIsNone(self.grant(self.heir))

    def test_the_administrator_cannot_name_themselves(self):
        OrganizationMember.objects.create(organization=self.org, user=self.admin)
        with self.assertRaises(ValidationError):
            offboarding.offboard(self.admin, self.leaver.pk, "root")
        self.assertIsNone(self.grant(self.admin))

    def test_a_successors_own_approval_rights_are_never_lowered(self):
        ApplicationGrant.objects.filter(user=self.leaver).update(can_approve=False)
        ApplicationGrant.objects.create(
            application=self.app, user=self.heir, role="viewer", can_approve=True
        )
        offboarding.offboard(self.admin, self.leaver.pk, "heir")
        self.assertTrue(self.grant(self.heir).can_approve)

    def test_only_a_platform_administrator_and_never_of_one(self):
        with self.assertRaises(PermissionDenied):
            offboarding.offboard(self.heir, self.leaver.pk, "heir")
        other = User.objects.create_user("root2", is_platform_admin=True)
        with self.assertRaises(PermissionDenied):
            offboarding.offboard(self.admin, other.pk, "heir")

    def test_the_page_says_what_moves_and_does_it(self):
        self.client.force_login(self.admin, backend="django.contrib.auth.backends.ModelBackend")
        url = reverse("person-offboard", args=[self.leaver.pk])
        page = self.client.get(url)
        self.assertContains(page, "Refunds (Northstar), with approval rights")
        self.assertContains(page, "stopped rather than moved")
        refused = self.client.post(url, {"successor": "nobody"})
        self.assertContains(refused, "Name an active user ID")
        done = self.client.post(url, {"successor": "heir"})
        self.assertRedirects(done, reverse("person", args=[self.leaver.pk]))
        self.assertEqual(self.grant(self.heir).role, "owner")

    def test_nobody_else_reaches_the_page(self):
        self.client.force_login(self.heir, backend="django.contrib.auth.backends.ModelBackend")
        response = self.client.get(reverse("person-offboard", args=[self.leaver.pk]))
        self.assertIn(response.status_code, {403, 404})
