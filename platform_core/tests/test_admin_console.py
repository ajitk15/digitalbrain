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
        console = self.client.get(reverse("platform-organizations"))
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
        console = self.client.get(reverse("platform-organizations"))
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


class PeopleTests(ConsoleTestCase):
    def setUp(self):
        super().setUp()
        from platform_core.models import ApiToken, ApplicationGrant, SignInIdentity

        self.sso = User.objects.create_user("sso.person", email="sso@example.test")
        self.sso.set_unusable_password()
        self.sso.save()
        SignInIdentity.objects.create(
            user=self.sso, tenant="t" * 36, subject="s" * 36, method="created"
        )
        ApplicationGrant.objects.create(application=self.app, user=self.alice, role="owner")
        ApiToken.objects.create(
            application=self.app, user=self.alice, name="ci", prefix="dbk_ci", digest="x"
        )

    def listed(self, **query):
        response = self.client.get(reverse("users"), query)
        self.assertEqual(response.status_code, 200)
        return {account.username for account in response.context["accounts"]}

    def lock(self, username):
        from axes.models import AccessAttempt
        from django.conf import settings

        AccessAttempt.objects.create(
            username=username,
            ip_address="192.0.2.1",
            user_agent="test",
            failures_since_start=settings.AXES_FAILURE_LIMIT,
            get_data="",
            post_data="",
        )

    def test_users_can_be_found_by_name_and_by_how_they_sign_in(self):
        self.assertEqual(self.listed(q="sso@"), {"sso.person"})
        self.assertEqual(self.listed(sign_in="microsoft"), {"sso.person"})
        self.assertNotIn("sso.person", self.listed(sign_in="password"))
        self.assertIn("alice", self.listed(sign_in="password"))

    def test_the_list_says_how_each_person_signs_in(self):
        response = self.client.get(reverse("users"))
        self.assertContains(response, "Microsoft")
        self.assertContains(response, reverse("person", args=[self.alice.pk]))

    def test_dormant_and_disabled_accounts_are_filtered(self):
        from datetime import timedelta

        from django.utils import timezone

        User.objects.filter(pk=self.alice.pk).update(
            last_login=timezone.now() - timedelta(days=120)
        )
        User.objects.filter(pk=self.sso.pk).update(is_active=False)
        self.assertIn("alice", self.listed(status="dormant"))
        self.assertEqual(self.listed(status="disabled"), {"sso.person"})

    def test_a_locked_account_is_shown_and_can_be_unlocked(self):
        self.lock("alice")
        self.assertEqual(self.listed(status="locked"), {"alice"})
        page = self.client.get(reverse("person", args=[self.alice.pk]))
        self.assertContains(page, "Locked out")
        self.client.post(reverse("person-unlock", args=[self.alice.pk]))
        self.assertEqual(self.listed(status="locked"), set())
        from platform_core.models import AuditEvent

        self.assertTrue(AuditEvent.objects.filter(action="account.unlocked").exists())

    def test_the_person_page_lists_access_as_names_only(self):
        response = self.client.get(reverse("person", args=[self.alice.pk]))
        self.assertContains(response, "Refunds")
        self.assertContains(response, "Application owner")
        self.assertContains(response, "dbk_ci")
        self.assertNotContains(response, reverse("application", args=[self.app.pk]))

    def test_only_a_platform_administrator_sees_people(self):
        self.login(self.alice)
        for url in (
            reverse("users"),
            reverse("person", args=[self.alice.pk]),
        ):
            self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(
            self.client.post(reverse("person-reset-link", args=[self.alice.pk])).status_code, 403
        )

    def test_disabling_from_the_person_page_comes_back_to_it(self):
        bob = User.objects.create_user("bob")
        response = self.client.post(
            reverse("user-status", args=[bob.pk]), {"state": "disable", "next": "person"}
        )
        self.assertRedirects(response, reverse("person", args=[bob.pk]))


class ResetLinkTests(ConsoleTestCase):
    def issue(self, user):
        response = self.client.post(reverse("person-reset-link", args=[user.pk]))
        self.assertEqual(response.status_code, 200)
        return response.context["reset_link"]

    def test_a_reset_link_sets_a_new_password_once(self):
        from platform_core.models import AuditEvent

        User.objects.filter(pk=self.alice.pk).update(must_change_password=True)
        link = self.issue(self.alice)
        self.assertTrue(AuditEvent.objects.filter(action="account.reset_link_issued").exists())
        self.client.logout()
        landing = self.client.get(link, follow=True)
        self.assertTrue(landing.context["validlink"])
        password = "Fixture-Reset-Password-5512!"
        done = self.client.post(
            landing.redirect_chain[-1][0],
            {"new_password1": password, "new_password2": password},
        )
        self.assertRedirects(done, reverse("login"), fetch_redirect_response=False)
        self.alice.refresh_from_db()
        self.assertTrue(self.alice.check_password(password))
        self.assertFalse(self.alice.must_change_password)
        self.assertTrue(AuditEvent.objects.filter(action="account.password_reset").exists())
        again = self.client.get(link, follow=True)
        self.assertFalse(again.context["validlink"])

    def test_the_link_is_shown_once_and_never_stored(self):
        from platform_core.models import AuditEvent

        link = self.issue(self.alice)
        token = link.rstrip("/").rsplit("/", 1)[-1]
        event = AuditEvent.objects.get(action="account.reset_link_issued")
        self.assertNotIn(token, str(event.details))
        self.assertNotContains(self.client.get(reverse("person", args=[self.alice.pk])), token)

    def test_no_link_for_a_platform_administrator_or_a_disabled_account(self):
        other = User.objects.create_user("other-admin", is_platform_admin=True)
        gone = User.objects.create_user("gone", is_active=False)
        for user in (other, gone, self.site):
            with self.subTest(user=user.username):
                response = self.client.post(reverse("person-reset-link", args=[user.pk]))
                self.assertEqual(response.status_code, 403)
