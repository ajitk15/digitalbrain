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
            reverse("documents", args=[(app or self.app).pk]),
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
            self.client.get(reverse("documents", args=[self.app.pk])), "requirements.md"
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
                    reverse("documents", args=[self.app.pk]),
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

    # ---- the application opens on Knowledge ----

    def test_opening_an_application_lands_on_knowledge(self):
        response = self.client.get(reverse("application", args=[self.app.pk]))
        self.assertRedirects(response, reverse("graph", args=[self.app.pk]))

    def test_with_knowledge_switched_off_it_falls_back_to_documents(self):
        """Knowledge is where an application opens, but not at the cost of a 403."""
        FeatureSwitch.objects.create(key="knowledge", enabled=False)
        response = self.client.get(reverse("application", args=[self.app.pk]))
        self.assertRedirects(response, reverse("documents", args=[self.app.pk]))

    def test_the_home_redirect_is_not_a_way_to_probe_for_applications(self):
        """Access is decided before the redirect, so an ungranted user still sees 404."""
        response = self.client.get(reverse("application", args=[self.other.pk]))
        self.assertEqual(response.status_code, 404)
        self.client.force_login(self.admin, backend="django.contrib.auth.backends.ModelBackend")
        self.assertEqual(
            self.client.get(reverse("application", args=[self.app.pk])).status_code, 404
        )

    def test_an_upload_returns_to_the_pane_it_came_from(self):
        documents_url = reverse("documents", args=[self.app.pk])
        self.assertRedirects(self.upload(), documents_url)
        response = self.client.post(
            documents_url,
            {
                "next": "graph",
                "file": SimpleUploadedFile("from-rail.md", b"# Rail"),
            },
        )
        self.assertRedirects(response, reverse("graph", args=[self.app.pk]))

    def test_a_forged_return_target_cannot_redirect_off_site(self):
        """`next` names a view; an unreversible string would become a location."""
        response = self.client.post(
            reverse("documents", args=[self.app.pk]),
            {
                "next": "https://example.invalid/steal",
                "file": SimpleUploadedFile("safe.md", b"# Safe"),
            },
        )
        self.assertRedirects(response, reverse("documents", args=[self.app.pk]))

    def test_organization_has_direct_application_links(self):
        response = self.client.get(reverse("organization", args=[self.app.organization_id]))
        self.assertContains(response, reverse("application", args=[self.app.pk]))
        self.assertContains(response, "Open knowledge")
        self.assertNotContains(response, f'href="{reverse("application", args=[self.other.pk])}"')

    def test_shared_navigation_on_application_pages(self):
        """Every application screen carries the breadcrumb and the same four tabs."""
        for name in ["graph", "usage", "application-access", "application-features"]:
            with self.subTest(page=name):
                response = self.client.get(reverse(name, args=[self.app.pk]))
                self.assertContains(response, 'aria-label="Breadcrumb"')
                self.assertContains(response, 'aria-label="Application menu"')
                # Documents now lives inside Knowledge. Icons are emitted
                # before the label precisely so these assertions keep working.
                self.assertContains(response, ">Knowledge</a>")
                self.assertContains(response, ">Chat</a>")
                self.assertContains(response, ">Settings</a>")

    def test_settings_screens_share_a_sub_navigation(self):
        for name in ["usage", "application-access", "application-features"]:
            with self.subTest(page=name):
                response = self.client.get(reverse(name, args=[self.app.pk]))
                self.assertContains(response, 'aria-label="Settings sections"')
                self.assertContains(response, ">Features</a>")

    def test_the_settings_tab_never_advertises_a_section_name(self):
        """A contributor reaching Settings via AI costs must still see "Settings"."""
        response = self.client.get(reverse("graph", args=[self.app.pk]))
        self.assertContains(response, ">Settings</a>")
        self.assertNotContains(response, ">AI costs</a>")


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class SourceListPagingTests(TestCase):
    """Telling someone their import is longer than the page they are looking at.

    A 27-file import showed 20 rows. The total was in the heading, several
    screens up behind an open intake panel, and the only way to the rest was a
    small right-aligned link below the twentieth row. The reasonable conclusion
    was that seven files had been dropped - and that is the conclusion that was
    drawn.
    """

    def setUp(self):
        DocumentTests.setUp(self)

    def add(self, count):
        for index in range(count):
            Document.objects.create(
                application=self.app,
                uploaded_by=self.owner,
                name=f"source-{index:03}.md",
                size=1,
                sha256=f"{index:064x}",
                status="ready",
                origin="upload",
            )

    def page(self, number=None):
        url = reverse("knowledge", args=[self.app.pk])
        return self.client.get(f"{url}?page={number}" if number else url).content.decode()

    def test_the_range_and_total_sit_beside_the_table(self):
        self.add(27)
        body = self.page()
        self.assertIn('class="table-summary"', body)
        summary = body.split('class="table-summary">')[1].split("</p>")[0]
        self.assertIn("1&ndash;20", summary)
        self.assertIn("27", summary)
        self.assertIn("page 1 of 2", summary)

    def test_the_second_page_says_which_rows_it_holds(self):
        self.add(27)
        summary = self.page(2).split('class="table-summary">')[1].split("</p>")[0]
        self.assertIn("21&ndash;27", summary)
        self.assertIn("page 2 of 2", summary)

    def test_a_single_page_does_not_claim_to_be_one_of_several(self):
        self.add(5)
        summary = self.page().split('class="table-summary">')[1].split("</p>")[0]
        self.assertIn("1&ndash;5", summary)
        self.assertNotIn("page 1 of", summary)

    def test_the_pager_names_the_page_rather_than_abbreviating_it(self):
        self.add(27)
        body = self.page()
        self.assertIn("Page 1 of 2", body)
        self.assertIn('href="?page=2"', body)

    def test_a_single_page_shows_no_pager(self):
        self.add(5)
        self.assertNotIn('class="pagination"', self.page())

    def test_the_pager_carries_the_search_across_pages(self):
        """Paging out of a search must not silently drop it."""
        self.add(27)
        url = reverse("knowledge", args=[self.app.pk])
        body = self.client.get(f"{url}?q=source-0").content.decode()
        if 'class="pagination"' in body:
            self.assertIn("q=source-0", body.split('class="pagination"')[1])
        summary = body.split('class="table-summary">')[1].split("</p>")[0]
        self.assertIn("matching", summary)
