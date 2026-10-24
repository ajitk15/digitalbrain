"""Leaving: tokens end with the account, and a membership can be taken away.

Disabling an account used to pause its API tokens and leave them active, so
re-enabling it brought every one back; and a membership could only be added,
so somebody who moved team kept every grant in the organization. These pin
both, and the guards a removal shares with a disable.
"""

from django.test import TestCase, override_settings
from django.urls import reverse

from platform_core import scim
from platform_core.models import (
    ApiToken,
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


def application(org, name):
    portfolio = Portfolio.objects.create(name=f"{name} portfolio", organization=org)
    product = Product.objects.create(name=f"{name} product", portfolio=portfolio)
    return Application.objects.create(name=name, product=product)


@override_settings(
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class MembershipRemovalTests(TestCase):
    def setUp(self):
        self.site = User.objects.create_user("siteadmin", is_platform_admin=True)
        self.admin = User.objects.create_user("admin")
        self.owner = User.objects.create_user("owner")
        self.bob = User.objects.create_user("bob")
        self.org = Organization.objects.create(name="Northstar")
        self.other = Organization.objects.create(name="Elsewhere")
        OrganizationMember.objects.create(organization=self.org, user=self.admin, is_admin=True)
        for person in (self.owner, self.bob):
            OrganizationMember.objects.create(organization=self.org, user=person)
        OrganizationMember.objects.create(organization=self.other, user=self.bob)
        self.app = application(self.org, "Refunds")
        self.foreign = application(self.other, "Checkout")
        ApplicationGrant.objects.create(application=self.app, user=self.owner, role="owner")
        ApplicationGrant.objects.create(application=self.app, user=self.bob, role="contributor")
        ApplicationGrant.objects.create(application=self.foreign, user=self.bob, role="owner")
        self.here = self.token(self.app, "here")
        self.there = self.token(self.foreign, "there")

    def token(self, app, name):
        return ApiToken.objects.create(
            application=app, user=self.bob, name=name, prefix=f"dbk_{name}", digest=name
        )

    def login(self, user):
        self.client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")

    def remove(self, user, as_user=None):
        self.login(as_user or self.admin)
        return self.client.post(
            reverse("organization-members", args=[self.org.pk]),
            {"action": "remove", "user": user.pk},
        )

    def refresh(self, *tokens):
        return [ApiToken.objects.get(pk=token.pk) for token in tokens]

    # ── tokens end with the account ───────────────────────────────────────────

    def test_disabling_an_account_revokes_its_tokens_for_good(self):
        ApplicationGrant.objects.filter(application=self.foreign).update(role="contributor")
        self.login(self.site)
        url = reverse("user-status", args=[self.bob.pk])
        self.client.post(url, {"state": "disable"})
        self.assertTrue(all(token.revoked_at for token in self.refresh(self.here, self.there)))
        self.client.post(url, {"state": "enable"})
        self.assertTrue(User.objects.get(pk=self.bob.pk).is_active)
        self.assertFalse(any(token.active for token in self.refresh(self.here, self.there)))
        reasons = AuditEvent.objects.filter(action="api_token.revoked").values_list(
            "details__reason", flat=True
        )
        self.assertEqual(list(reasons), ["account disabled", "account disabled"])

    def test_a_refused_disable_revokes_nothing(self):
        # Bob is the only owner of Checkout, so the disable is refused.
        self.login(self.site)
        self.client.post(reverse("user-status", args=[self.bob.pk]), {"state": "disable"})
        self.assertFalse(any(token.revoked_at for token in self.refresh(self.here, self.there)))

    def test_directory_deprovisioning_revokes_tokens(self):
        ProvisionedUser.objects.create(user=self.bob)
        scim.apply(self.bob, {"is_active": False}, scim.provisioner())
        self.assertTrue(all(token.revoked_at for token in self.refresh(self.here, self.there)))

    # ── removing a member ─────────────────────────────────────────────────────

    def test_removing_a_member_takes_their_grants_and_tokens_in_that_organization(self):
        response = self.remove(self.bob)
        self.assertRedirects(response, reverse("organization-members", args=[self.org.pk]))
        self.assertFalse(
            OrganizationMember.objects.filter(organization=self.org, user=self.bob).exists()
        )
        self.assertFalse(ApplicationGrant.objects.filter(application=self.app, user=self.bob))
        here, there = self.refresh(self.here, self.there)
        self.assertIsNotNone(here.revoked_at)
        # The other organization is untouched.
        self.assertIsNone(there.revoked_at)
        self.assertTrue(ApplicationGrant.objects.filter(application=self.foreign, user=self.bob))
        self.assertTrue(
            OrganizationMember.objects.filter(organization=self.other, user=self.bob).exists()
        )
        event = AuditEvent.objects.get(action="organization.member_removed")
        self.assertEqual(event.organization, self.org)
        self.assertEqual(event.details, {"grants": 1, "tokens": 1})
        self.assertTrue(AuditEvent.objects.filter(action="application.access_revoked").exists())

    def test_the_last_owner_of_an_application_cannot_be_removed(self):
        self.remove(self.owner)
        self.assertTrue(
            OrganizationMember.objects.filter(organization=self.org, user=self.owner).exists()
        )
        self.assertTrue(ApplicationGrant.objects.filter(application=self.app, user=self.owner))
        self.assertFalse(AuditEvent.objects.filter(action="organization.member_removed"))

    def test_an_owner_can_go_once_someone_else_owns_it(self):
        ApplicationGrant.objects.filter(application=self.app, user=self.bob).update(role="owner")
        self.remove(self.owner)
        self.assertFalse(
            OrganizationMember.objects.filter(organization=self.org, user=self.owner).exists()
        )

    def test_the_last_administrator_cannot_be_removed(self):
        response = self.remove(self.admin)
        self.assertRedirects(response, reverse("organization-members", args=[self.org.pk]))
        self.assertTrue(
            OrganizationMember.objects.filter(organization=self.org, user=self.admin).exists()
        )

    def test_an_administrator_who_removes_themselves_goes_to_the_dashboard(self):
        OrganizationMember.objects.filter(organization=self.org, user=self.owner).update(
            is_admin=True
        )
        response = self.remove(self.admin)
        self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)
        self.assertFalse(
            OrganizationMember.objects.filter(organization=self.org, user=self.admin).exists()
        )

    def test_only_an_organization_administrator_can_remove(self):
        self.assertEqual(self.remove(self.bob, as_user=self.owner).status_code, 404)
        self.assertEqual(self.remove(self.bob, as_user=self.site).status_code, 404)
        self.assertTrue(
            OrganizationMember.objects.filter(organization=self.org, user=self.bob).exists()
        )

    def test_someone_from_another_organization_is_not_found(self):
        stranger = User.objects.create_user("stranger")
        OrganizationMember.objects.create(organization=self.other, user=stranger)
        self.assertEqual(self.remove(stranger).status_code, 404)

    def test_the_members_screen_offers_removal(self):
        self.login(self.admin)
        response = self.client.get(reverse("organization-members", args=[self.org.pk]))
        self.assertContains(response, 'value="remove"', count=3)
