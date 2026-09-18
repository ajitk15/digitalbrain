import tempfile
from pathlib import Path

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from platform_core.documents import DocumentForm, document_path
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


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class SourceListTopPagerTests(TestCase):
    """A pager above the table as well as below it.

    With twenty rows between them, reaching page two meant scrolling past every
    row of page one first.
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

    def body(self):
        return self.client.get(reverse("knowledge", args=[self.app.pk])).content.decode()

    def test_a_pager_appears_above_and_below_the_table(self):
        self.add(27)
        body = self.body()
        self.assertEqual(body.count('class="pagination"'), 2)
        self.assertIn("Pagination, above the table", body)
        self.assertIn("Pagination, below the table", body)

    def test_the_two_pagers_are_told_apart_for_a_screen_reader(self):
        """Two landmarks with one name is a worse list than one landmark."""
        self.add(27)
        body = self.body()
        self.assertNotIn('aria-label="Pagination"', body)

    def test_the_top_pager_sits_with_the_summary(self):
        self.add(27)
        toolbar = self.body().split('class="table-toolbar"')[1].split("</div>")[0]
        self.assertIn("table-summary", toolbar)
        self.assertIn('href="?page=2"', toolbar)

    def test_a_single_page_shows_neither_pager(self):
        self.add(5)
        body = self.body()
        self.assertNotIn('class="pagination"', body)
        self.assertIn('class="table-toolbar"', body)

    def test_both_pagers_carry_the_search(self):
        self.add(27)
        url = reverse("knowledge", args=[self.app.pk])
        body = self.client.get(f"{url}?q=source-0").content.decode()
        for marker in ("Pagination, above the table", "Pagination, below the table"):
            if marker in body:
                self.assertIn("q=source-0", body.split(marker)[1][:400])


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class OriginOutcomeTests(TestCase):
    """An origin row says what arrived, not only that it was checked.

    A link whose only file failed to convert used to read "In sync - 1 file",
    which is two true statements arranged to mean something false. The status
    answers "has the origin moved?" and still does; what was missing is the
    other fact, so it is said beside it rather than folded into it.
    """

    def setUp(self):
        DocumentTests.setUp(self)

    def origin_with(self, *statuses):
        from platform_core.knowledge_sources import register

        source = register(self.owner, self.app, "link", "https://example.com/docs")
        for index, status in enumerate(statuses):
            Document.objects.create(
                application=self.app,
                uploaded_by=self.owner,
                source=source,
                name=f"file-{index}.html",
                size=0 if status == "failed" else 10,
                sha256=f"{index:064d}",
                status=status,
                origin="link",
                source_url="https://example.com/docs",
            )
        return source

    def test_an_origin_whose_only_file_failed_says_nothing_imported(self):
        self.origin_with("failed")
        response = self.client.get(reverse("documents", args=[self.app.pk]))
        self.assertContains(response, "nothing imported")

    def test_a_partial_failure_is_counted_rather_than_generalised(self):
        self.origin_with("ready", "failed", "failed")
        response = self.client.get(reverse("documents", args=[self.app.pk]))
        self.assertContains(response, "2 failed to convert")
        self.assertNotContains(response, "nothing imported")

    def test_an_origin_that_converted_cleanly_says_nothing_extra(self):
        self.origin_with("ready", "ready")
        response = self.client.get(reverse("documents", args=[self.app.pk]))
        self.assertNotContains(response, "failed to convert")
        self.assertNotContains(response, "nothing imported")

    def test_the_status_still_means_only_what_it_meant(self):
        """Drift and conversion are separate facts and stay separate."""
        from platform_core.models import KnowledgeSource

        source = self.origin_with("failed")
        source.refresh_from_db()
        # Untouched by the conversion: a newly registered source has not been
        # checked for drift yet, and a file failing to convert is not a drift
        # check. Whatever the status says, it says it about the origin.
        self.assertEqual(source.status, KnowledgeSource.Status.UNKNOWN)


class DocumentRetryTests(TestCase):
    """Putting a failed document back in the queue.

    A conversion can fail for reasons that have nothing to do with the file. The
    only way back used to be deleting the source and importing it again - which,
    for one file out of a directory import, meant re-importing the directory.
    """

    def setUp(self):
        DocumentTests.setUp(self)

    def failed(self, *, stored=True, url="", user=None):
        doc = Document.objects.create(
            application=self.app,
            uploaded_by=user or self.owner,
            name="beaten.pptx",
            size=10,
            sha256="f" * 64,
            status="failed",
            origin="github" if url else "upload",
            source_url=url,
            conversion_error="Conversion could not finish. Check access and retry.",
        )
        if stored:
            path = document_path(self.app, doc.pk)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"stored")
        return doc

    def retry(self, doc):
        return self.client.post(reverse("document-retry", args=[self.app.pk, doc.pk]))

    def test_a_stored_document_goes_back_to_conversion(self):
        doc = self.failed()
        self.retry(doc)
        doc.refresh_from_db()
        self.assertEqual(doc.status, "queued")
        self.assertEqual(doc.conversion_error, "")

    def test_a_link_with_nothing_stored_goes_back_to_download(self):
        """It never got the bytes, so re-converting them is not the retry it needs."""
        doc = self.failed(stored=False, url="https://raw.example/x.pptx")
        self.retry(doc)
        doc.refresh_from_db()
        self.assertEqual(doc.status, "pending")

    def test_a_document_with_neither_bytes_nor_a_link_is_refused(self):
        doc = self.failed(stored=False)
        self.retry(doc)
        doc.refresh_from_db()
        self.assertEqual(doc.status, "failed")

    def test_only_a_failed_document_can_be_retried(self):
        doc = self.failed()
        Document.objects.filter(pk=doc.pk).update(status="ready")
        self.assertEqual(self.retry(doc).status_code, 404)

    def test_a_viewer_cannot_retry(self):
        doc = self.failed()
        self.client.force_login(self.viewer, backend="django.contrib.auth.backends.ModelBackend")
        self.assertEqual(self.retry(doc).status_code, 403)
        doc.refresh_from_db()
        self.assertEqual(doc.status, "failed")

    def test_retrying_is_recorded(self):
        doc = self.failed()
        self.retry(doc)
        self.assertTrue(AuditEvent.objects.filter(action="document.retried").exists())

    def test_a_second_press_does_not_queue_a_second_attempt(self):
        """The guard is on `failed`, so the second press finds nothing to move."""
        doc = self.failed()
        self.retry(doc)
        self.assertEqual(self.retry(doc).status_code, 404)

    def test_the_control_appears_only_on_a_failed_row(self):
        doc = self.failed()
        body = self.client.get(reverse("knowledge", args=[self.app.pk])).content.decode()
        self.assertIn(reverse("document-retry", args=[self.app.pk, doc.pk]), body)
        Document.objects.filter(pk=doc.pk).update(status="ready")
        body = self.client.get(reverse("knowledge", args=[self.app.pk])).content.decode()
        self.assertNotIn(reverse("document-retry", args=[self.app.pk, doc.pk]), body)

    def test_retry_is_a_post_not_a_link(self):
        """A GET that changes state is one a crawler or a prefetch can fire."""
        doc = self.failed()
        self.assertEqual(
            self.client.get(reverse("document-retry", args=[self.app.pk, doc.pk])).status_code, 405
        )
