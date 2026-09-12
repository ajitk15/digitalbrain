import tempfile
from pathlib import Path

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from platform_core.documents import DocumentForm
from platform_core.models import (
    Application,
    ApplicationGrant,
    AuditEvent,
    Document,
    FeatureSwitch,
    Organization,
    OrganizationMember,
    Portfolio,
    Product,
    User,
)


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class DocumentTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner")
        self.viewer = User.objects.create_user("viewer")
        self.admin = User.objects.create_user("platform", is_platform_admin=True)
        org = Organization.objects.create(name="Example")
        for user in [self.owner, self.viewer]:
            OrganizationMember.objects.create(
                organization=org, user=user, is_admin=user == self.owner
            )
        portfolio = Portfolio.objects.create(name="P", organization=org)
        product = Product.objects.create(name="Product", portfolio=portfolio)
        self.app = Application.objects.create(name="Primary", product=product)
        self.other = Application.objects.create(name="Other", product=product)
        ApplicationGrant.objects.create(application=self.app, user=self.owner, role="owner")
        ApplicationGrant.objects.create(application=self.app, user=self.viewer, role="viewer")
        self.client.force_login(self.owner, backend="django.contrib.auth.backends.ModelBackend")
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = override_settings(BASE_DIR=Path(self.directory.name))
        self.config.enable()
        self.addCleanup(self.config.disable)

    def upload(self, app=None):
        return self.client.post(
            reverse("application", args=[(app or self.app).pk]),
            {
                "file": SimpleUploadedFile(
                    "requirements.md", b"# Requirements\nKeep applications isolated."
                )
            },
        )

    def test_upload_stores_original_privately_and_audits(self):
        self.assertEqual(self.upload().status_code, 302)
        doc = Document.objects.get()
        self.assertEqual(doc.status, "quarantined")
        stored = (
            Path(self.directory.name)
            / ".runtime/documents"
            / str(self.app.organization_id)
            / str(self.app.pk)
            / f"{doc.pk}.quarantine"
        )
        self.assertEqual(stored.read_bytes(), b"# Requirements\nKeep applications isolated.")
        self.assertTrue(AuditEvent.objects.filter(action="document.uploaded").exists())
        self.assertContains(
            self.client.get(reverse("application", args=[self.app.pk])), "requirements.md"
        )

    def test_viewer_and_platform_admin_cannot_upload(self):
        self.client.force_login(self.viewer, backend="django.contrib.auth.backends.ModelBackend")
        self.assertEqual(self.upload().status_code, 403)
        self.client.force_login(self.admin, backend="django.contrib.auth.backends.ModelBackend")
        self.assertEqual(self.upload().status_code, 404)
        self.assertFalse(Document.objects.exists())

    def test_cross_application_upload_and_detail_are_blocked(self):
        self.assertEqual(self.upload(self.other).status_code, 404)
        self.upload()
        doc = Document.objects.get()
        ApplicationGrant.objects.create(application=self.other, user=self.owner, role="owner")
        self.assertEqual(
            self.client.get(reverse("document-detail", args=[self.other.pk, doc.pk])).status_code,
            404,
        )

    def test_revocation_blocks_document_metadata(self):
        self.upload()
        doc = Document.objects.get()
        ApplicationGrant.objects.filter(application=self.app, user=self.owner).delete()
        self.assertEqual(
            self.client.get(reverse("document-detail", args=[self.app.pk, doc.pk])).status_code, 404
        )

    def test_arbitrary_formats_preserve_original_bytes_in_quarantine(self):
        for name, value in [
            ("budget.xlsx", b"PK spreadsheet"),
            ("slides.pptx", b"PK presentation"),
            ("legacy.doc", b"legacy binary"),
            ("diagram.png", b"image bytes"),
            ("data.csv", b"name,value\nrow,1"),
            ("archive.zip", b"PK archive"),
            ("README", b"extensionless"),
            ("custom.vendor", b"custom format"),
            ("binary.txt", b"a\x00b"),
            ("legacy.txt", b"caf\xe9"),
            ("run.exe", b"MZ stored only"),
        ]:
            with self.subTest(name=name):
                response = self.client.post(
                    reverse("application", args=[self.app.pk]),
                    {
                        "file": SimpleUploadedFile(name, value),
                    },
                )
                self.assertEqual(response.status_code, 302)
                doc = Document.objects.get(name=name)
                self.assertEqual(doc.status, "quarantined")
                stored = (
                    Path(self.directory.name)
                    / ".runtime/documents"
                    / str(self.app.organization_id)
                    / str(self.app.pk)
                    / f"{doc.pk}.quarantine"
                )
                self.assertEqual(stored.read_bytes(), value)

    def test_empty_and_oversized_files_remain_rejected(self):
        for value in [b"", b"x" * (20 * 1024 * 1024 + 1)]:
            form = DocumentForm(files={"file": SimpleUploadedFile("arbitrary.custom", value)})
            self.assertFalse(form.is_valid())

    def test_upload_flag_enforced(self):
        FeatureSwitch.objects.create(key="document_uploads", enabled=False)
        self.assertEqual(self.upload().status_code, 403)
        self.assertFalse(Document.objects.exists())

    @override_settings(PRODUCTION=True)
    def test_production_intake_fails_closed_until_private_storage_is_connected(self):
        self.assertEqual(self.upload().status_code, 403)

    def test_organization_has_direct_application_links(self):
        response = self.client.get(reverse("organization", args=[self.app.organization_id]))
        self.assertContains(response, reverse("application", args=[self.app.pk]))
        self.assertContains(response, "Open documents")
        self.assertNotContains(response, f'href="{reverse("application", args=[self.other.pk])}"')

    def test_shared_navigation_on_application_pages(self):
        for name in ["application", "usage", "application-access", "application-features"]:
            response = self.client.get(reverse(name, args=[self.app.pk]))
            self.assertContains(response, 'aria-label="Breadcrumb"')
            self.assertContains(response, ">Documents</a>")
