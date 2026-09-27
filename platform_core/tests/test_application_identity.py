"""Owner-managed application identity stays optional and application scoped."""

import io

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from platform_core.models import Application, ApplicationFeature, ApplicationLogo

from . import test_documents


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class ApplicationIdentityTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.url = reverse("application-identity", args=[self.app.pk])

    def upload(self):
        image = Image.new("RGB", (600, 400), "#24746b")
        output = io.BytesIO()
        image.save(output, format="JPEG")
        return SimpleUploadedFile("identity.jpg", output.getvalue(), content_type="image/jpeg")

    def test_overview_distinguishes_engineering_and_operations_with_labels_and_icons(self):
        ApplicationFeature.objects.create(application=self.app, key="service_ops", enabled=False)
        page = self.client.get(reverse("dashboard"))
        self.assertContains(page, "purpose-engineering")
        self.assertContains(page, "Engineering")
        self.assertContains(page, self.url)
        ApplicationFeature.objects.filter(application=self.app, key="service_ops").update(
            enabled=True
        )
        for key in ("code_factory", "code_graph"):
            ApplicationFeature.objects.create(application=self.app, key=key, enabled=False)
        page = self.client.get(reverse("dashboard"))
        self.assertContains(page, "purpose-operations")
        self.assertContains(page, "Operations")

    def test_owner_can_choose_icon_then_restore_initial(self):
        self.assertRedirects(
            self.client.post(self.url, {"style": "icon", "icon": "pulse"}),
            reverse("dashboard"),
        )
        self.app.refresh_from_db()
        self.assertEqual(self.app.identity_icon, "pulse")
        self.assertContains(self.client.get(reverse("dashboard")), "icon or logo")
        self.client.post(self.url, {"style": "letter"})
        self.app.refresh_from_db()
        self.assertEqual(self.app.identity_icon, "")
        self.assertEqual(self.app.logo_digest, "")

    def test_uploaded_logo_is_sanitized_private_and_can_be_replaced_by_icon(self):
        self.client.post(self.url, {"style": "logo", "logo": self.upload()})
        self.app.refresh_from_db()
        self.assertTrue(self.app.logo_digest)
        logo_url = reverse("application-logo", args=[self.app.pk])
        image = self.client.get(logo_url)
        self.assertEqual(image.status_code, 200)
        self.assertEqual(image["Content-Type"], "image/png")
        self.assertEqual(image["Cache-Control"], "no-store")
        with Image.open(io.BytesIO(image.content)) as decoded:
            self.assertLessEqual(max(decoded.size), 256)
        self.assertContains(self.client.get(reverse("dashboard")), logo_url)
        self.assertEqual(
            self.client.get(logo_url, HTTP_IF_NONE_MATCH=image["ETag"]).status_code, 304
        )
        self.client.post(self.url, {"style": "icon", "icon": "code"})
        self.app.refresh_from_db()
        self.assertEqual(self.app.logo_digest, "")
        self.assertFalse(ApplicationLogo.objects.filter(application=self.app).exists())
        self.assertEqual(self.client.get(logo_url).status_code, 404)

    def test_invalid_upload_and_missing_logo_do_not_change_identity(self):
        missing = self.client.post(self.url, {"style": "logo"})
        self.assertContains(missing, "Choose an image to upload.")
        invalid = self.client.post(
            self.url,
            {"style": "logo", "logo": SimpleUploadedFile("image.png", b"not an image")},
        )
        self.assertContains(invalid, "valid, undamaged image")
        self.app.refresh_from_db()
        self.assertFalse(self.app.logo_digest)

    def test_only_owner_can_edit_and_foreign_application_is_hidden(self):
        self.client.force_login(self.viewer, backend="django.contrib.auth.backends.ModelBackend")
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(
            self.client.post(self.url, {"style": "icon", "icon": "code"}).status_code, 403
        )
        self.assertEqual(
            self.client.get(reverse("application-identity", args=[self.other.pk])).status_code,
            404,
        )
        self.assertNotContains(self.client.get(reverse("dashboard")), "Change icon or logo")

    def test_onboarding_offers_appearance_without_making_it_required(self):
        page = self.client.get(reverse("onboarding", args=[self.app.pk]))
        self.assertContains(page, "Make this application easy to recognize")
        self.assertContains(page, "Optional")
        self.assertEqual(Application.objects.get(pk=self.app.pk).identity_icon, "")
