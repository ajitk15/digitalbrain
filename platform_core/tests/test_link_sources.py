from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from platform_core.fetching import FetchError
from platform_core.link_sources import download, submit
from platform_core.models import CodeRepository, Document

from . import test_documents


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class LinkSubmissionTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)

    def test_a_page_is_queued_not_downloaded_during_the_request(self):
        """A slow or dead host must never hold a web request open."""
        with patch("platform_core.link_sources.fetch") as fetcher:
            created = submit(self.owner, self.app.pk, "https://example.com/guide")
        fetcher.assert_not_called()
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].status, "pending")
        self.assertEqual(created[0].origin, "link")
        self.assertEqual(created[0].source_url, "https://example.com/guide")

    def test_the_same_link_is_not_imported_twice(self):
        submit(self.owner, self.app.pk, "https://example.com/guide")
        with self.assertRaises(ValidationError):
            submit(self.owner, self.app.pk, "https://example.com/guide")
        self.assertEqual(Document.objects.filter(origin="link").count(), 1)

    def test_a_viewer_cannot_import_a_link(self):
        with self.assertRaises(PermissionDenied):
            submit(self.viewer, self.app.pk, "https://example.com/guide")

    def test_a_refused_scheme_never_creates_a_document(self):
        for value in ["file:///etc/passwd", "javascript:alert(1)", "data:text/html,x"]:
            with self.subTest(value=value), self.assertRaises(FetchError):
                submit(self.owner, self.app.pk, value)
        self.assertFalse(Document.objects.filter(origin="link").exists())

    def test_a_github_repo_queues_its_readme(self):
        with patch(
            "platform_core.link_sources.github_plan",
            return_value=[("docs-README.md", "https://raw.githubusercontent.com/a/docs/main/README.md")],
        ):
            created = submit(self.owner, self.app.pk, "https://github.com/a/docs")
        self.assertEqual(created[0].origin, "github")
        self.assertEqual(created[0].name, "docs-README.md")
        # Importing documents from a repository does not put that repository
        # into Code Graph. A documentation repository is not this application's
        # code, and a row nobody asked for is one more thing a run has to
        # choose between - which is how a ticket ends up reasoning about no
        # code at all. Code Graph rows come from the Code Graph screen.
        self.assertFalse(CodeRepository.objects.filter(application=self.app).exists())

    def test_a_docs_tree_queues_every_file_it_resolved(self):
        files = [(f"docs-guide{n}.md", f"https://raw.example/{n}.md") for n in range(5)]
        with patch("platform_core.link_sources.github_plan", return_value=files):
            created = submit(self.owner, self.app.pk, "https://github.com/a/docs/tree/main/docs")
        self.assertEqual(len(created), 5)
        self.assertEqual(Document.objects.filter(status="pending").count(), 5)

    @override_settings(IMPORT_MAX_FILES=30)
    def test_a_submission_is_capped_at_the_configured_number(self):
        files = [(f"f{n}.md", f"https://raw.example/{n}.md") for n in range(80)]
        with patch("platform_core.link_sources.github_plan", return_value=files):
            created = submit(self.owner, self.app.pk, "https://github.com/a/docs/tree/main/docs")
        self.assertEqual(len(created), 30)

    @override_settings(IMPORT_MAX_FILES=30)
    def test_a_capped_submission_says_what_it_left_behind(self):
        """A backstop that fires quietly is a bug nobody finds."""
        files = [(f"f{n}.md", f"https://raw.example/{n}.md") for n in range(80)]
        notes = []
        with patch("platform_core.link_sources.github_plan", return_value=files):
            submit(self.owner, self.app.pk, "https://github.com/a/docs/tree/main/docs", notes)
        self.assertEqual(notes, ["Only the first 30 of 80 files were queued."])

    def test_an_uncapped_submission_reports_nothing(self):
        files = [(f"f{n}.md", f"https://raw.example/{n}.md") for n in range(5)]
        notes = []
        with patch("platform_core.link_sources.github_plan", return_value=files):
            submit(self.owner, self.app.pk, "https://github.com/a/docs/tree/main/docs", notes)
        self.assertEqual(notes, [])


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class LinkDownloadTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.doc = submit(self.owner, self.app.pk, "https://example.com/guide")[0]

    def test_a_successful_download_hands_over_to_the_conversion_queue(self):
        body = b"<html><body><h1>Queue guide</h1></body></html>"
        with patch(
            "platform_core.link_sources.fetch",
            return_value=(body, "text/html", "guide.html", "https://example.com/guide"),
        ):
            download(self.doc)
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.status, "queued")
        self.assertEqual(self.doc.size, len(body))
        self.assertEqual(len(self.doc.sha256), 64)

    def test_a_failed_download_is_recorded_on_the_row_not_raised(self):
        """One unreachable link must never stop the queue for everything else."""
        with patch(
            "platform_core.link_sources.fetch",
            side_effect=FetchError("example.com could not be reached."),
        ):
            download(self.doc)
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.status, "failed")
        self.assertIn("could not be reached", self.doc.conversion_error)

    def test_an_unexpected_error_still_fails_safely(self):
        with patch("platform_core.link_sources.fetch", side_effect=RuntimeError("boom")):
            download(self.doc)
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.status, "failed")
        # The internal message is never surfaced.
        self.assertNotIn("boom", self.doc.conversion_error)

    def test_a_document_already_claimed_is_not_downloaded_twice(self):
        Document.objects.filter(pk=self.doc.pk).update(status="fetching")
        with patch("platform_core.link_sources.fetch") as fetcher:
            self.assertFalse(download(self.doc))
        fetcher.assert_not_called()


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class LinkViewTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)

    def test_the_rail_offers_the_paste_box(self):
        response = self.client.get(reverse("graph", args=[self.app.pk]))
        self.assertContains(response, 'name="url"')
        self.assertContains(response, 'value="link"')

    def test_posting_a_link_queues_it_and_reports_back(self):
        response = self.client.post(
            reverse("documents", args=[self.app.pk]),
            {"action": "link", "url": "https://example.com/guide"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Document.objects.filter(origin="link", status="pending").count(), 1)

    def test_a_rejected_link_is_reported_on_the_form_not_a_500(self):
        response = self.client.post(
            reverse("documents", args=[self.app.pk]),
            {"action": "link", "url": "file:///etc/passwd"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "http and https")
        self.assertFalse(Document.objects.filter(origin="link").exists())

    def test_a_blocked_address_is_refused_while_the_user_is_still_looking(self):
        """The worker would refuse it anyway, but "Queued" then "failed" is poor feedback."""
        import socket

        records = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.1", 443))]
        with patch("socket.getaddrinfo", return_value=records):
            with self.assertRaises(FetchError) as raised:
                submit(self.owner, self.app.pk, "http://internal.example/secret")
        self.assertIn("private or reserved", str(raised.exception))
        self.assertFalse(Document.objects.filter(source_url__contains="internal.example").exists())
