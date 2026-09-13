import io
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from PIL import Image

from platform_core.forms import BrandingForm
from platform_core.models import (
    AIUsage,
    Application,
    ApplicationFeature,
    ApplicationGrant,
    AuditEvent,
    Branding,
    FeatureSwitch,
    Organization,
    OrganizationMember,
    Portfolio,
    Product,
)
from platform_core.services import change_grant, record_ai_usage


def png():
    data = io.BytesIO()
    Image.new("RGBA", (80, 30), (40, 50, 190, 128)).save(data, "PNG")
    return data.getvalue()


@override_settings(
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class SecurityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        user = get_user_model()
        cls.admin = user.objects.create_user(
            "platform", password="not-a-real-password", is_platform_admin=True
        )
        cls.owner = user.objects.create_user("owner", password="not-a-real-password")
        cls.other = user.objects.create_user("other", password="not-a-real-password")
        cls.viewer = user.objects.create_user("viewer", password="not-a-real-password")
        cls.org = Organization.objects.create(name="Northstar")
        cls.org_b = Organization.objects.create(name="Other company")
        for person in [cls.owner, cls.viewer, cls.other]:
            OrganizationMember.objects.create(
                organization=cls.org, user=person, is_admin=person == cls.owner
            )
        OrganizationMember.objects.create(organization=cls.org_b, user=cls.other, is_admin=True)
        portfolio = Portfolio.objects.create(name="Commerce", organization=cls.org)
        product = Product.objects.create(name="Payments", portfolio=portfolio)
        cls.app = Application.objects.create(name="Refunds", product=product)
        cls.app_b = Application.objects.create(name="Checkout", product=product)
        foreign = Portfolio.objects.create(name="Foreign", organization=cls.org_b)
        cls.foreign_product = Product.objects.create(name="Foreign product", portfolio=foreign)
        ApplicationGrant.objects.create(application=cls.app, user=cls.owner, role="owner")
        ApplicationGrant.objects.create(application=cls.app, user=cls.viewer, role="viewer")
        ApplicationGrant.objects.create(application=cls.app_b, user=cls.other, role="owner")

    def login(self, user):
        self.client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")

    def receipt(self, **kwargs):
        defaults = dict(
            actor=self.owner,
            application_id=self.app.pk,
            provider="test-provider",
            model="test-model",
            request_id="receipt-1",
            input_tokens=100,
            output_tokens=20,
            amount=Decimal("0.00123456"),
            currency="USD",
            estimated=False,
        )
        defaults.update(kwargs)
        return record_ai_usage(**defaults)

    def test_login_required_and_post_only_logout(self):
        self.assertEqual(self.client.get("/").status_code, 302)
        self.login(self.owner)
        self.assertEqual(self.client.get(reverse("logout")).status_code, 405)

    def test_platform_admin_has_no_implicit_application_access(self):
        self.login(self.admin)
        self.assertEqual(self.client.get(reverse("platform-console")).status_code, 200)
        for name in ["application", "usage", "application-access", "application-features"]:
            self.assertEqual(self.client.get(reverse(name, args=[self.app.pk])).status_code, 404)

    def test_org_admin_has_no_implicit_application_access(self):
        self.login(self.owner)
        self.assertEqual(
            self.client.get(reverse("application", args=[self.app_b.pk])).status_code, 404
        )

    def test_cross_application_and_cross_org_hierarchy_denied(self):
        self.login(self.owner)
        self.assertEqual(
            self.client.get(reverse("application", args=[self.app.pk])).status_code, 200
        )
        self.assertEqual(self.client.get(reverse("usage", args=[self.app_b.pk])).status_code, 404)
        response = self.client.post(
            reverse("application-new", args=[self.foreign_product.pk]),
            {"name": "Intrusion", "owner": self.owner.pk},
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Application.objects.filter(name="Intrusion").exists())

    def test_removed_membership_revokes_even_existing_grant(self):
        self.login(self.viewer)
        OrganizationMember.objects.filter(user=self.viewer).delete()
        self.assertEqual(
            self.client.get(reverse("application", args=[self.app.pk])).status_code, 404
        )

    def test_disabled_organization_and_application_block_access(self):
        self.login(self.owner)
        self.app.active = False
        self.app.save()
        self.assertEqual(
            self.client.get(reverse("application", args=[self.app.pk])).status_code, 404
        )
        self.app.active = True
        self.app.save()
        self.org.active = False
        self.org.save()
        self.assertEqual(
            self.client.get(reverse("application", args=[self.app.pk])).status_code, 404
        )

    def test_viewer_cannot_manage_grants_or_features(self):
        self.login(self.viewer)
        self.assertEqual(
            self.client.get(reverse("application-access", args=[self.app.pk])).status_code, 403
        )
        self.assertEqual(
            self.client.post(
                reverse("application-features", args=[self.app.pk]),
                {"feature": "usage_reports", "state": "off"},
            ).status_code,
            403,
        )

    def test_last_owner_and_approval_escalation_rejected(self):
        with self.assertRaises(ValidationError):
            change_grant(self.owner, self.app.pk, self.owner, "viewer")
        with self.assertRaises(PermissionDenied):
            change_grant(self.owner, self.app.pk, self.viewer, "viewer", can_approve=True)
        self.assertEqual(
            ApplicationGrant.objects.get(application=self.app, user=self.owner).role, "owner"
        )

    def test_grant_revocation_effective_on_next_request(self):
        change_grant(self.owner, self.app.pk, self.viewer, "viewer", revoke=True)
        self.login(self.viewer)
        self.assertEqual(
            self.client.get(reverse("application", args=[self.app.pk])).status_code, 404
        )
        self.assertTrue(AuditEvent.objects.filter(action="application.access_revoked").exists())

    def test_non_member_cannot_receive_grant(self):
        outsider = get_user_model().objects.create_user("outsider")
        with self.assertRaises(ValidationError):
            change_grant(self.owner, self.app.pk, outsider, "viewer")

    def test_logo_requires_platform_role_and_csrf(self):
        self.login(self.owner)
        response = self.client.post(
            reverse("branding"),
            {"logo": SimpleUploadedFile("image.png", png(), content_type="image/png")},
        )
        self.assertEqual(response.status_code, 403)
        secured = Client(enforce_csrf_checks=True)
        secured.force_login(self.admin, backend="django.contrib.auth.backends.ModelBackend")
        self.assertEqual(secured.post(reverse("branding"), {}).status_code, 403)
        self.assertFalse(Branding.objects.exists())

    def test_logo_replacement_preserves_alpha_and_is_audited(self):
        self.login(self.admin)
        payload = png()
        response = self.client.post(
            reverse("branding"),
            {"logo": SimpleUploadedFile("image.png", payload, content_type="image/png")},
        )
        self.assertRedirects(response, reverse("branding"))
        result = self.client.get(reverse("logo"))
        with Image.open(io.BytesIO(result.content)) as image:
            self.assertEqual(image.getpixel((0, 0)), (40, 50, 190, 128))
        self.assertEqual(result["Content-Type"], "image/png")
        self.assertTrue(
            AuditEvent.objects.filter(action="branding.updated", actor=self.admin).exists()
        )

    def test_logo_invalid_inputs_do_not_replace_existing_brand(self):
        for name, content in [
            ("logo.svg", b'<svg onload="alert(1)"></svg>'),
            ("fake.png", b"not a png"),
            ("big.png", b"x" * (2 * 1024 * 1024 + 1)),
        ]:
            with self.subTest(name=name):
                form = BrandingForm(files={"logo": SimpleUploadedFile(name, content)})
                self.assertFalse(form.is_valid())

    def test_logo_strips_trailing_payload_and_supports_etag(self):
        self.login(self.admin)
        self.client.post(
            reverse("branding"),
            {"logo": SimpleUploadedFile("logo.png", png() + b"<script>bad()</script>")},
        )
        result = self.client.get(reverse("logo"))
        self.assertNotIn(b"<script>", result.content)
        self.assertEqual(
            self.client.get(reverse("logo"), HTTP_IF_NONE_MATCH=result["ETag"]).status_code, 304
        )

    def test_cost_receipt_is_exact_and_idempotent(self):
        first, second = self.receipt(), self.receipt()
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(AIUsage.objects.count(), 1)
        self.assertEqual(first.amount, Decimal("0.00123456"))
        self.assertEqual(AuditEvent.objects.filter(action="ai.usage_recorded").count(), 1)
        with self.assertRaises(ValidationError):
            self.receipt(output_tokens=21)

    def test_invalid_accounting_is_rejected(self):
        for overrides in [
            {"amount": "-1"},
            {"amount": "NaN"},
            {"input_tokens": -1},
            {"output_tokens": True},
            {"currency": "usd"},
            {"amount": "0.000000001"},
            {"request_id": ""},
        ]:
            with self.subTest(overrides=overrides), self.assertRaises(ValidationError):
                self.receipt(**overrides)
        self.assertFalse(AIUsage.objects.exists())

    def test_cross_application_accounting_denied(self):
        from django.http import Http404

        with self.assertRaises(Http404):
            self.receipt(application_id=self.app_b.pk)
        self.assertFalse(AIUsage.objects.exists())

    def test_cost_report_scopes_rows_and_separates_currencies(self):
        self.receipt()
        self.receipt(request_id="eur", currency="EUR", amount="1")
        self.receipt(
            actor=self.other,
            application_id=self.app_b.pk,
            model="secret-foreign-model",
            request_id="foreign",
            amount="9999",
        )
        self.login(self.owner)
        response = self.client.get(reverse("usage", args=[self.app.pk]))
        self.assertContains(response, "USD")
        self.assertContains(response, "EUR")
        self.assertNotContains(response, "secret-foreign-model")
        self.assertEqual(len(response.context["totals"]), 2)

    def test_feature_disable_enforced_server_side_accounting_continues(self):
        FeatureSwitch.objects.create(key="usage_reports", enabled=False)
        ApplicationFeature.objects.create(application=self.app, key="usage_reports", enabled=True)
        self.login(self.owner)
        self.assertEqual(self.client.get(reverse("usage", args=[self.app.pk])).status_code, 404)
        self.receipt()
        self.assertEqual(AIUsage.objects.count(), 1)
        self.assertNotContains(
            self.client.get(reverse("application", args=[self.app.pk])), ">AI costs<"
        )

    def test_unknown_features_cannot_be_enabled(self):
        self.login(self.admin)
        response = self.client.post(
            reverse("features"), {"feature": "unknown-feature", "state": "on"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(FeatureSwitch.objects.filter(key="unknown-feature").exists())

    def test_feature_change_is_audited(self):
        self.login(self.admin)
        self.client.post(reverse("features"), {"feature": "usage_reports", "state": "off"})
        self.assertTrue(AuditEvent.objects.filter(action="feature.disabled").exists())
        self.assertFalse(FeatureSwitch.objects.get(key="usage_reports").enabled)

    def test_audit_does_not_disclose_other_organization_events(self):
        AuditEvent.objects.create(
            actor=self.other,
            organization=self.org_b,
            action="foreign.secret_event",
            resource_id="foreign",
        )
        self.login(self.owner)
        self.assertNotContains(self.client.get(reverse("audit")), "foreign.secret_event")

    def test_costs_are_not_recorded_if_audit_write_fails(self):
        with patch("platform_core.services.audit", side_effect=RuntimeError("database failure")):
            with self.assertRaises(RuntimeError):
                self.receipt()
        self.assertFalse(AIUsage.objects.exists())

    def test_all_authorized_screens_render(self):
        self.login(self.owner)
        urls = [
            reverse("dashboard"),
            reverse("organization", args=[self.org.pk]),
            reverse("organization-members", args=[self.org.pk]),
            reverse("application", args=[self.app.pk]),
            reverse("application-access", args=[self.app.pk]),
            reverse("application-features", args=[self.app.pk]),
            reverse("usage", args=[self.app.pk]),
            reverse("audit"),
        ]
        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertIn("no-store", response["Cache-Control"])
                self.assertIn("frame-ancestors 'none'", response["Content-Security-Policy"])
        self.login(self.admin)
        for name in ["platform-console", "branding", "features", "organization-new"]:
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_content_security_policy_is_exactly_the_agreed_directives(self):
        """Pinned deliberately.

        connect-src 'self' exists so the chat page can open a same-origin
        EventSource. Nothing else may be widened without changing this test, and
        in particular no directive may gain 'unsafe-inline' or an external origin.
        """
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(
            response["Content-Security-Policy"],
            "default-src 'none'; img-src 'self'; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; form-action 'self'; base-uri 'none'; "
            "frame-ancestors 'none'",
        )

    def test_form_output_escapes_resource_names(self):
        self.app.name = '<img src=x onerror="alert(1)">'
        self.app.save()
        self.login(self.owner)
        response = self.client.get(reverse("dashboard"))
        self.assertNotContains(response, "<img src=x")
        self.assertContains(response, "&lt;img src=x")

    def test_health_checks(self):
        self.assertEqual(self.client.get(reverse("live")).status_code, 200)
        self.assertEqual(self.client.get(reverse("ready")).status_code, 200)

    def test_login_lockout(self):
        for _ in range(5):
            response = self.client.post(
                reverse("login"), {"username": "viewer", "password": "wrong"}
            )
        self.assertEqual(response.status_code, 429)
        response = self.client.post(
            reverse("login"), {"username": "viewer", "password": "not-a-real-password"}
        )
        self.assertEqual(response.status_code, 429)
