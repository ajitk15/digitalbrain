"""The administration pages: what needs attention, sign-in, operations."""

from django.test import TestCase, override_settings
from django.urls import reverse

from platform_core import authenticator, console
from platform_core.models import (
    Application,
    ApplicationGrant,
    Invitation,
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
class PlatformPageTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("root", is_platform_admin=True)
        self.owner = User.objects.create_user("owner")
        self.org = Organization.objects.create(name="Northstar")
        OrganizationMember.objects.create(organization=self.org, user=self.owner, is_admin=True)
        portfolio = Portfolio.objects.create(name="Commerce", organization=self.org)
        product = Product.objects.create(name="Payments", portfolio=portfolio)
        self.app = Application.objects.create(name="Refunds", product=product)
        ApplicationGrant.objects.create(application=self.app, user=self.owner, role="owner")
        self.client.force_login(self.admin, backend="django.contrib.auth.backends.ModelBackend")

    def labels(self):
        return {row["label"] for row in console.attention()}

    def test_a_healthy_tree_needs_nothing_about_people(self):
        labels = self.labels()
        self.assertNotIn("Organizations with no administrator", labels)
        self.assertNotIn("Applications nobody owns", labels)

    def test_losing_the_last_administrator_and_owner_needs_attention(self):
        self.owner.is_active = False
        self.owner.save()
        labels = self.labels()
        self.assertIn("Organizations with no administrator", labels)
        self.assertIn("Applications nobody owns", labels)
        page = self.client.get(reverse("platform-console"))
        self.assertContains(page, "Refunds (Northstar)")
        organizations = self.client.get(reverse("platform-organizations"))
        self.assertContains(organizations, "No administrator")
        self.assertContains(organizations, "Applications nobody owns")

    def test_an_owner_who_left_the_organization_owns_nothing(self):
        OrganizationMember.objects.filter(user=self.owner).delete()
        self.assertIn("Applications nobody owns", self.labels())

    def test_invitations_waiting_are_a_note(self):
        Invitation.objects.create(
            application=self.app, invitee="new@example.com", role="viewer", invited_by=self.owner
        )
        row = next(r for r in console.attention() if r["label"].startswith("Invitations"))
        self.assertEqual((row["count"], row["level"]), (1, "note"))

    def test_sign_in_page_says_who_has_an_authenticator(self):
        seed = authenticator.start(self.admin)
        authenticator.confirm(self.admin, authenticator.code_at(seed, authenticator.current_step()))
        page = self.client.get(reverse("platform-security"))
        self.assertContains(page, "1 of 2 active accounts added one")
        self.assertContains(page, "Authenticator app")
        self.assertContains(page, "Nobody is locked out")

    def test_operations_lists_every_lane_and_objective(self):
        from platform_core.document_worker import LANES

        page = self.client.get(reverse("platform-operations"))
        for name, _, _ in LANES:
            self.assertContains(page, f"<strong>{name}</strong>")
        self.assertContains(page, "Service levels")

    def test_every_page_carries_the_administration_tabs(self):
        for name in ("platform-console", "users", "platform-organizations", "platform-security",
                     "platform-operations", "platform-settings", "branding", "audit"):
            with self.subTest(page=name):
                self.assertContains(self.client.get(reverse(name)), 'aria-label="Administration"')

    def test_nobody_else_sees_them(self):
        self.client.force_login(self.owner, backend="django.contrib.auth.backends.ModelBackend")
        for name in ("platform-organizations", "platform-security", "platform-operations",
                     "platform-settings"):
            with self.subTest(page=name):
                self.assertIn(self.client.get(reverse(name)).status_code, {403, 404})
        self.assertNotContains(self.client.get(reverse("audit")), 'aria-label="Administration"')
