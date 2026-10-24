"""The owner's Access screen, a person's own My access, and an empty dashboard."""

from django.test import TestCase, override_settings
from django.urls import reverse

from platform_core.models import (
    Application,
    ApplicationGrant,
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
class AccessScreenTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner")
        self.bob = User.objects.create_user("bob")
        self.carol = User.objects.create_user("carol")
        self.org = Organization.objects.create(name="Northstar")
        for person in (self.owner, self.bob, self.carol):
            OrganizationMember.objects.create(organization=self.org, user=person)
        portfolio = Portfolio.objects.create(name="Commerce", organization=self.org)
        product = Product.objects.create(name="Payments", portfolio=portfolio)
        self.app = Application.objects.create(name="Refunds", product=product)
        self.other_app = Application.objects.create(name="Checkout", product=product)
        self.mine = ApplicationGrant.objects.create(
            application=self.app, user=self.owner, role="owner", can_approve=True
        )
        ApplicationGrant.objects.create(application=self.app, user=self.bob, role="viewer")
        self.url = reverse("application-access", args=[self.app.pk])
        self.login(self.owner)

    def login(self, user):
        self.client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")

    def grant(self, user):
        return ApplicationGrant.objects.filter(application=self.app, user=user).first()

    def test_every_grant_is_a_row_that_can_be_changed_in_place(self):
        response = self.client.get(self.url)
        self.assertContains(response, 'form="grant-', count=5)
        saved = self.client.post(
            self.url, {"user": self.bob.pk, "role": "contributor", "action": "save"}
        )
        self.assertRedirects(saved, self.url)
        self.assertEqual(self.grant(self.bob).role, "contributor")

    def test_giving_and_revoking_access_come_back_to_the_screen(self):
        given = self.client.post(
            self.url, {"user": self.carol.pk, "role": "viewer", "action": "save"}
        )
        self.assertRedirects(given, self.url)
        revoked = self.client.post(
            self.url, {"user": self.carol.pk, "role": "viewer", "action": "revoke"}
        )
        self.assertRedirects(revoked, self.url)
        self.assertIsNone(self.grant(self.carol))

    def test_an_owner_without_approval_keeps_what_others_hold(self):
        self.mine.can_approve = False
        self.mine.save()
        ApplicationGrant.objects.filter(application=self.app, user=self.bob).update(
            can_approve=True
        )
        # The disabled checkbox posts nothing.
        self.client.post(self.url, {"user": self.bob.pk, "role": "contributor", "action": "save"})
        self.assertTrue(self.grant(self.bob).can_approve)
        self.assertEqual(self.grant(self.bob).role, "contributor")
        # And still cannot hand it to someone new.
        self.client.post(
            self.url,
            {"user": self.carol.pk, "role": "viewer", "can_approve": "on", "action": "save"},
        )
        self.assertFalse(self.grant(self.carol).can_approve)

    def test_the_last_owner_is_kept_and_the_screen_says_why(self):
        response = self.client.post(
            self.url, {"user": self.owner.pk, "role": "viewer", "action": "save"}
        )
        self.assertContains(response, "Keep at least one active application owner.")
        self.assertEqual(self.grant(self.owner).role, "owner")

    def test_my_access_lists_only_my_own_live_grants(self):
        self.login(self.bob)
        response = self.client.get(reverse("my-access"))
        self.assertContains(response, reverse("application", args=[self.app.pk]))
        self.assertContains(response, "Viewer")
        self.assertNotContains(response, "Checkout")
        self.assertContains(response, "<code>bob</code>")
        self.app.active = False
        self.app.save()
        self.assertNotContains(self.client.get(reverse("my-access")), "Refunds")

    def test_an_empty_dashboard_says_which_user_id_to_give(self):
        stranger = User.objects.create_user("stranger")
        self.login(stranger)
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "<code>stranger</code>")
