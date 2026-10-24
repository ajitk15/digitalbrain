"""Capabilities, access that ends on a date, and owners confirming the list."""

from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from platform_core import access_review, console
from platform_core.models import (
    AIUsage,
    Application,
    ApplicationGrant,
    AuditEvent,
    Organization,
    OrganizationMember,
    Portfolio,
    Product,
    User,
)
from platform_core.policy import CAPABILITIES, application_for, applications_for
from platform_core.services import change_grant
from platform_core.workbench import access


@override_settings(
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class AccessReviewTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner")
        self.temp = User.objects.create_user("temp")
        self.org = Organization.objects.create(name="Northstar")
        for person in (self.owner, self.temp):
            OrganizationMember.objects.create(organization=self.org, user=person)
        portfolio = Portfolio.objects.create(name="Commerce", organization=self.org)
        product = Product.objects.create(name="Payments", portfolio=portfolio)
        self.app = Application.objects.create(name="Refunds", product=product)
        ApplicationGrant.objects.create(
            application=self.app, user=self.owner, role="owner", can_approve=True
        )
        self.url = reverse("application-access", args=[self.app.pk])

    def grant(self, user):
        return ApplicationGrant.objects.filter(application=self.app, user=user).first()

    def give(self, role, days):
        return ApplicationGrant.objects.create(
            application=self.app,
            user=self.temp,
            role=role,
            expires_at=timezone.now() + timedelta(days=days),
        )

    def login(self, user):
        self.client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")

    def test_the_registry_names_every_role_and_approval(self):
        self.assertEqual(
            set(CAPABILITIES), set(ApplicationGrant.Role.values) | {"can_approve"}
        )

    def test_the_registry_says_what_the_checks_allow(self):
        # Viewer: reads, never writes. Contributor: writes, never owner screens.
        self.give("viewer", 30)
        access(self.temp, self.app.pk, "knowledge")
        with self.assertRaises(PermissionDenied):
            access(self.temp, self.app.pk, "knowledge", write=True)
        ApplicationGrant.objects.filter(user=self.temp).update(role="contributor")
        access(self.temp, self.app.pk, "knowledge", write=True)
        with self.assertRaises(PermissionDenied):
            application_for(self.temp, self.app.pk, owner=True)
        application_for(self.owner, self.app.pk, owner=True)

    def test_a_grant_opens_nothing_once_it_has_expired(self):
        self.give("contributor", 30)
        self.assertTrue(applications_for(self.temp).exists())
        ApplicationGrant.objects.filter(user=self.temp).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )
        self.assertFalse(applications_for(self.temp).exists())
        with self.assertRaises(Http404):
            application_for(self.temp, self.app.pk)

    def test_an_owner_never_expires_and_a_past_date_is_refused(self):
        soon = timezone.now() + timedelta(days=3)
        with self.assertRaises(ValidationError):
            change_grant(self.owner, self.app.pk, self.temp, "owner", expires_at=soon)
        with self.assertRaises(ValidationError):
            change_grant(
                self.owner, self.app.pk, self.temp, "viewer",
                expires_at=timezone.now() - timedelta(days=1),
            )
        change_grant(self.owner, self.app.pk, self.temp, "viewer", expires_at=soon)
        self.assertEqual(self.grant(self.temp).expires_at, soon)

    def test_the_maintenance_lane_revokes_what_expired_and_audits_it(self):
        past = self.give("viewer", 1)
        ApplicationGrant.objects.filter(pk=past.pk).update(
            expires_at=timezone.now() - timedelta(hours=1)
        )
        self.assertTrue(access_review.expire_due())
        self.assertIsNone(self.grant(self.temp))
        self.assertIsNotNone(self.grant(self.owner))
        event = AuditEvent.objects.get(action="application.access_revoked")
        self.assertEqual(event.actor.username, access_review.EXPIRY)
        self.assertFalse(event.actor.is_active)
        self.assertFalse(event.actor.has_usable_password())
        self.assertEqual(event.details["reason"], "expired")
        self.assertFalse(access_review.expire_due())

    def test_a_person_holding_the_expiry_name_pauses_it(self):
        User.objects.create_user(access_review.EXPIRY, password="a real password 12345")
        grant = self.give("viewer", 1)
        ApplicationGrant.objects.filter(pk=grant.pk).update(
            expires_at=timezone.now() - timedelta(hours=1)
        )
        self.assertFalse(access_review.expire_due())
        self.assertIsNotNone(self.grant(self.temp))
        self.assertFalse(applications_for(self.temp).exists())

    def test_the_access_screen_sets_an_expiry_and_records_a_review(self):
        self.login(self.owner)
        day = (timezone.now() + timedelta(days=10)).date()
        saved = self.client.post(
            self.url,
            {"user": self.temp.pk, "role": "viewer", "expires_on": day.isoformat(),
             "action": "save"},
        )
        self.assertRedirects(saved, self.url)
        self.assertEqual(self.grant(self.temp).expires_at.date(), day)
        page = self.client.get(self.url)
        self.assertContains(page, "What each role can do")
        self.assertContains(page, "Nobody has confirmed this list yet")
        reviewed = self.client.post(self.url, {"action": "review"})
        self.assertRedirects(reviewed, self.url)
        self.app.refresh_from_db()
        self.assertEqual(self.app.access_reviewed_by, self.owner)
        self.assertTrue(AuditEvent.objects.filter(action="application.access_reviewed").exists())
        self.assertContains(self.client.get(self.url), "Last confirmed")

    def test_only_an_owner_reviews(self):
        self.give("contributor", 30)
        with self.assertRaises(PermissionDenied):
            access_review.review(self.temp, self.app.pk)

    def test_an_unreviewed_application_comes_due(self):
        self.assertFalse(access_review.due_for_review().exists())
        Application.objects.filter(pk=self.app.pk).update(
            created_at=timezone.now() - timedelta(days=access_review.REVIEW_DAYS + 1)
        )
        self.assertTrue(access_review.due_for_review().exists())
        self.assertTrue(any("due a review" in row["label"] for row in console.attention()))
        access_review.review(self.owner, self.app.pk)
        self.assertFalse(access_review.due_for_review().exists())

    def test_my_access_says_when_access_ends(self):
        self.give("viewer", 10)
        self.login(self.temp)
        page = self.client.get(reverse("my-access"))
        self.assertContains(page, (timezone.now() + timedelta(days=10)).strftime("%b %d, %Y"))
        self.assertContains(page, "What each role can do")

    def test_spend_is_counted_by_organization(self):
        AIUsage.objects.create(
            application=self.app, actor=self.owner, purpose="chat", provider="openai",
            model="m", amount="0.25", currency="USD", request_id="r1",
            input_tokens=1, output_tokens=1,
        )
        rows = list(console.spend_by_organization())
        self.assertEqual(rows[0]["application__product__portfolio__organization__name"],
                         "Northstar")
        self.assertEqual(rows[0]["calls"], 1)
