"""Emptying an organization between demonstrations.

Nothing else in this platform deletes an application, so most of what these
pin is the fence around the one thing that does: who may, where it is offered
at all, what has to be typed, and what survives regardless.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from platform_core import demo_reset
from platform_core.models import (
    Application,
    ApplicationGrant,
    AuditEvent,
    Document,
    FactoryRun,
    KnowledgeEntry,
    Organization,
    OrganizationMember,
    Portfolio,
    Product,
    User,
)

SETTINGS = dict(
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    ALLOW_DEMO_RESET=True,
)


@override_settings(**SETTINGS)
class DemoResetTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("siteadmin", is_platform_admin=True)
        self.member = User.objects.create_user("member")
        self.org = Organization.objects.create(name="ACME")
        OrganizationMember.objects.create(organization=self.org, user=self.admin, is_admin=True)
        OrganizationMember.objects.create(organization=self.org, user=self.member)
        portfolio = Portfolio.objects.create(organization=self.org, name="Integrated Care")
        product = Product.objects.create(portfolio=portfolio, name="Care Coordination")
        self.app = Application.objects.create(product=product, name="CarePath")
        ApplicationGrant.objects.create(application=self.app, user=self.member, role="owner")
        KnowledgeEntry.objects.create(
            application=self.app, author=self.member, title="A source", content="x", digest="d"
        )
        Document.objects.create(
            application=self.app, uploaded_by=self.member, name="a.md", size=1, sha256="s"
        )
        FactoryRun.objects.create(
            application=self.app, requested_by=self.member, number=1, ticket_title="A ticket"
        )
        AuditEvent.objects.create(
            actor=self.member,
            organization=self.org,
            action="knowledge.created",
            resource_id=str(self.app.pk),
        )
        self.client.force_login(self.admin, backend="django.contrib.auth.backends.ModelBackend")

    # ---- what it removes, and what it does not ----

    def test_it_empties_the_hierarchy_and_everything_under_it(self):
        demo_reset.reset(self.admin, self.org, "ACME")
        self.assertEqual(Application.objects.count(), 0)
        self.assertEqual(Product.objects.count(), 0)
        self.assertEqual(Portfolio.objects.count(), 0)
        for model in (KnowledgeEntry, Document, FactoryRun):
            with self.subTest(model=model.__name__):
                self.assertEqual(model.objects.count(), 0)

    def test_the_organization_its_people_and_their_accounts_survive(self):
        """What a demonstration fills goes; who gives it does not."""
        demo_reset.reset(self.admin, self.org, "ACME")
        self.assertTrue(Organization.objects.filter(pk=self.org.pk).exists())
        self.assertEqual(OrganizationMember.objects.filter(organization=self.org).count(), 2)
        self.assertEqual(User.objects.count(), 2)

    def test_audit_rows_naming_a_removed_application_go_with_it(self):
        """A reference nobody can follow is worse than no reference."""
        demo_reset.reset(self.admin, self.org, "ACME")
        self.assertFalse(AuditEvent.objects.filter(resource_id=str(self.app.pk)).exists())
        # And the reset itself is recorded.
        self.assertTrue(AuditEvent.objects.filter(action="organization.reset").exists())

    def test_another_organization_is_untouched(self):
        other = Organization.objects.create(name="Other")
        portfolio = Portfolio.objects.create(organization=other, name="Theirs")
        product = Product.objects.create(portfolio=portfolio, name="Theirs")
        kept = Application.objects.create(product=product, name="Keep me")
        demo_reset.reset(self.admin, self.org, "ACME")
        self.assertTrue(Application.objects.filter(pk=kept.pk).exists())

    # ---- the fence ----

    def test_the_name_must_be_typed_exactly(self):
        for typed in ("", "acme", "ACME ", None):
            with self.subTest(typed=typed):
                with self.assertRaises(ValidationError):
                    demo_reset.reset(self.admin, self.org, typed)
        self.assertEqual(Application.objects.count(), 1)

    def test_only_a_platform_administrator_may(self):
        with self.assertRaises(PermissionDenied):
            demo_reset.reset(self.member, self.org, "ACME")
        self.assertEqual(Application.objects.count(), 1)

    @override_settings(ALLOW_DEMO_RESET=False)
    def test_it_is_refused_where_it_is_not_enabled(self):
        with self.assertRaises(PermissionDenied):
            demo_reset.reset(self.admin, self.org, "ACME")
        self.assertEqual(Application.objects.count(), 1)

    @override_settings(ALLOW_DEMO_RESET=False)
    def test_the_console_does_not_offer_it_where_it_is_not_enabled(self):
        page = self.client.get(reverse("platform-console"))
        self.assertNotContains(page, "Reset for a demonstration")

    def test_the_console_says_what_would_go_before_it_goes(self):
        page = self.client.get(reverse("platform-console"))
        self.assertContains(page, "Reset for a demonstration")
        self.assertContains(page, "There is no undo")
        self.assertContains(page, "1 applications")

    # ---- through the screen ----

    def test_the_form_empties_it_and_says_what_it_did(self):
        response = self.client.post(
            reverse("organization-reset", args=[self.org.pk]), {"confirm": "ACME"}, follow=True
        )
        self.assertContains(response, "was reset")
        self.assertEqual(Application.objects.count(), 0)

    def test_a_mistyped_name_changes_nothing_and_says_so(self):
        response = self.client.post(
            reverse("organization-reset", args=[self.org.pk]), {"confirm": "acme"}, follow=True
        )
        self.assertContains(response, "Type the organization&#x27;s name exactly")
        self.assertEqual(Application.objects.count(), 1)

    def test_it_cannot_be_reached_by_following_a_link(self):
        """POST only: there is no page to arrive at by accident."""
        self.assertEqual(
            self.client.get(reverse("organization-reset", args=[self.org.pk])).status_code, 405
        )
