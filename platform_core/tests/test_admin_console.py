"""The platform administrator's screens: confirming a disable, finding people,
unlocking and resetting them, and what one person's page shows."""

from django.test import TestCase, override_settings
from django.urls import reverse

from platform_core.models import (
    Application,
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
class ConsoleTestCase(TestCase):
    def setUp(self):
        self.site = User.objects.create_user("siteadmin", is_platform_admin=True)
        self.alice = User.objects.create_user("alice", password="not-a-real-password")
        self.org = Organization.objects.create(name="Northstar")
        OrganizationMember.objects.create(organization=self.org, user=self.alice, is_admin=True)
        portfolio = Portfolio.objects.create(name="Commerce", organization=self.org)
        self.product = Product.objects.create(name="Payments", portfolio=portfolio)
        self.app = Application.objects.create(name="Refunds", product=self.product)
        Application.objects.create(name="Checkout", product=self.product)
        self.login(self.site)

    def login(self, user):
        self.client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")


class DisableConfirmationTests(ConsoleTestCase):
    def test_disabling_an_organization_asks_first_and_says_what_it_turns_off(self):
        url = reverse("organization-status", args=[self.org.pk])
        response = self.client.get(url)
        self.assertContains(response, "Disable Northstar?")
        self.assertContains(response, "the 2 applications under this organization")
        self.assertContains(response, "No work is waiting to start")
        self.org.refresh_from_db()
        self.assertTrue(self.org.active)
        self.client.post(url, {"state": "disable"})
        self.org.refresh_from_db()
        self.assertFalse(self.org.active)

    def test_the_console_and_structure_open_the_question_instead_of_disabling(self):
        console = self.client.get(reverse("platform-console"))
        self.assertContains(
            console, f'href="{reverse("organization-status", args=[self.org.pk])}" data-modal'
        )
        structure = self.client.get(reverse("organization-structure", args=[self.org.pk]))
        self.assertContains(
            structure, f'href="{reverse("level-status", args=["application", self.app.pk])}"'
        )

    def test_an_application_disable_is_asked_about_on_its_own(self):
        response = self.client.get(reverse("level-status", args=["application", self.app.pk]))
        self.assertContains(response, "Nobody can use this application")
        self.assertContains(response, reverse("organization-structure", args=[self.org.pk]))

    def test_enabling_is_still_one_click(self):
        self.org.active = False
        self.org.save()
        console = self.client.get(reverse("platform-console"))
        self.assertContains(console, 'value="enable"')

    def test_only_a_platform_administrator_is_asked(self):
        self.login(self.alice)
        self.assertEqual(
            self.client.get(reverse("level-status", args=["product", self.product.pk])).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(reverse("organization-status", args=[self.org.pk])).status_code, 403
        )
