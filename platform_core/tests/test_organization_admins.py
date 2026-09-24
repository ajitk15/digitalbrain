"""Organization administrators: several at creation, and changeable afterwards.

Creating an organization used to take exactly one administrator and offered no
way to change it, so a site admin could neither add a second nor replace one who
left. These pin both screens, and that neither widens access.
"""

from django.test import TestCase, override_settings
from django.urls import reverse

from platform_core.models import AuditEvent, Organization, OrganizationMember, User


@override_settings(
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class OrganizationAdministratorTests(TestCase):
    def setUp(self):
        self.site = User.objects.create_user("siteadmin", is_platform_admin=True)
        self.alice = User.objects.create_user("alice")
        self.bob = User.objects.create_user("bob")
        self.carol = User.objects.create_user("carol")
        self.client.force_login(self.site, backend="django.contrib.auth.backends.ModelBackend")

    def admins(self, org):
        return set(
            OrganizationMember.objects.filter(organization=org, is_admin=True).values_list(
                "user__username", flat=True
            )
        )

    def test_create_with_several_administrators(self):
        response = self.client.post(
            reverse("organization-new"),
            {"name": "Acme", "administrators": [self.alice.pk, self.bob.pk]},
        )
        self.assertRedirects(response, reverse("platform-console"))
        org = Organization.objects.get(name="Acme")
        self.assertEqual(self.admins(org), {"alice", "bob"})
        self.assertFalse(OrganizationMember.objects.filter(user=self.site).exists())

    def test_create_requires_an_administrator(self):
        response = self.client.post(reverse("organization-new"), {"name": "Acme"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "at least one administrator")
        self.assertFalse(Organization.objects.exists())

    def test_change_administrators_after_creation(self):
        org = Organization.objects.create(name="Acme")
        OrganizationMember.objects.create(organization=org, user=self.alice, is_admin=True)
        OrganizationMember.objects.create(organization=org, user=self.carol)
        url = reverse("organization-administrators", args=[org.pk])
        self.assertContains(self.client.get(url), "alice")
        response = self.client.post(url, {"administrators": [self.bob.pk, self.carol.pk]})
        self.assertRedirects(response, reverse("platform-console"))
        self.assertEqual(self.admins(org), {"bob", "carol"})
        # Demoted, not removed: membership is a separate decision.
        self.assertTrue(
            OrganizationMember.objects.filter(organization=org, user=self.alice).exists()
        )
        actions = set(AuditEvent.objects.values_list("action", flat=True))
        self.assertIn("organization.admin_added", actions)
        self.assertIn("organization.admin_removed", actions)
        self.assertFalse(OrganizationMember.objects.filter(user=self.site).exists())

    def test_cannot_remove_the_last_administrator(self):
        org = Organization.objects.create(name="Acme")
        OrganizationMember.objects.create(organization=org, user=self.alice, is_admin=True)
        response = self.client.post(reverse("organization-administrators", args=[org.pk]), {})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.admins(org), {"alice"})

    def test_deactivated_administrator_is_kept_unless_unticked(self):
        org = Organization.objects.create(name="Acme")
        OrganizationMember.objects.create(organization=org, user=self.alice, is_admin=True)
        self.alice.is_active = False
        self.alice.save()
        url = reverse("organization-administrators", args=[org.pk])
        self.client.post(url, {"administrators": [self.alice.pk, self.bob.pk]})
        self.assertEqual(self.admins(org), {"alice", "bob"})

    def test_only_a_platform_administrator_may(self):
        org = Organization.objects.create(name="Acme")
        OrganizationMember.objects.create(organization=org, user=self.alice, is_admin=True)
        self.client.force_login(self.alice, backend="django.contrib.auth.backends.ModelBackend")
        url = reverse("organization-administrators", args=[org.pk])
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, {"administrators": [self.bob.pk]}).status_code, 403)
        self.assertEqual(self.admins(org), {"alice"})
