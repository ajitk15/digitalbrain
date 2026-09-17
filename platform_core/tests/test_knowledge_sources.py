"""Sources that are remembered, checked on a schedule, and synced only on request.

The rule these exist to hold: drift is detected automatically because detecting
it is free and safe, and nothing is synchronised automatically because acting on
it is neither.
"""

from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import PermissionDenied
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from platform_core.knowledge_sources import (
    check,
    process_next_source,
    readable_name,
    register,
    resync,
)
from platform_core.models import Document, KnowledgeSource

from . import test_documents

TREE = "https://github.com/ajitk15/digitalbrain/tree/main/demo-artifacts/docs"
FILE_ZERO = "https://raw.example/0.md"
FILE_ONE = "https://raw.example/1.md"


def settings_for_tests(cls):
    return override_settings(
        DOCUMENT_AUTO_CONVERT=False,
        STORAGES={
            "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}
        },
        PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    )(cls)


@settings_for_tests
class SourceRegistrationTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)

    def test_a_name_identifies_the_directory_not_just_the_repository(self):
        self.assertEqual(
            readable_name("github", TREE), "ajitk15/digitalbrain · demo-artifacts/docs"
        )

    def test_importing_the_same_address_twice_adds_to_one_source(self):
        """Two sources for one origin would drift apart and disagree."""
        first = register(self.owner, self.app, "github", TREE)
        second = register(self.owner, self.app, "github", TREE)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(KnowledgeSource.objects.filter(application=self.app).count(), 1)

    def test_an_import_records_the_address_it_came_from(self):
        from platform_core.link_sources import submit

        files = [(f"docs/f{n}.md", f"https://raw.example/{n}.md") for n in range(3)]
        with patch("platform_core.link_sources.github_plan", return_value=files):
            created = submit(self.owner, self.app.pk, TREE)
        source = KnowledgeSource.objects.get(application=self.app)
        self.assertEqual(source.url, TREE)
        self.assertTrue(all(document.source_id == source.pk for document in created))

    def test_documents_imported_before_sources_existed_are_adopted(self):
        """A source that cannot see its own files cannot say what has gone."""
        from platform_core.link_sources import submit

        earlier = Document.objects.create(
            application=self.app,
            uploaded_by=self.owner,
            name="f0.md",
            size=1,
            sha256="0" * 64,
            status="ready",
            origin="github",
            source_url=FILE_ZERO,
        )
        self.assertIsNone(earlier.source_id)
        files = [("docs/f0.md", FILE_ZERO), ("docs/f1.md", FILE_ONE)]
        with patch("platform_core.link_sources.github_plan", return_value=files):
            submit(self.owner, self.app.pk, TREE)
        earlier.refresh_from_db()
        self.assertIsNotNone(earlier.source_id)


@settings_for_tests
class DriftDetectionTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.source = register(self.owner, self.app, "github", TREE)

    def signalling(self, value):
        return patch("platform_core.knowledge_sources.signal", return_value=value)

    def test_the_first_signal_is_recorded_without_announcing_a_change(self):
        """Nothing to compare against is not the same as something moved."""
        with self.signalling("abc123"):
            self.assertEqual(check(self.source, self.app), KnowledgeSource.Status.READY)
        self.source.refresh_from_db()
        self.assertEqual(self.source.fingerprint, "abc123")
        self.assertEqual(self.source.drift_summary, "")

    def test_an_unchanged_signal_leaves_the_source_in_sync(self):
        KnowledgeSource.objects.filter(pk=self.source.pk).update(fingerprint="abc123")
        self.source.refresh_from_db()
        with self.signalling("abc123"):
            self.assertEqual(check(self.source, self.app), KnowledgeSource.Status.READY)

    def test_a_changed_signal_marks_the_source_out_of_sync(self):
        KnowledgeSource.objects.filter(pk=self.source.pk).update(fingerprint="abc123")
        self.source.refresh_from_db()
        with self.signalling("def456"):
            self.assertEqual(check(self.source, self.app), KnowledgeSource.Status.STALE)
        self.source.refresh_from_db()
        self.assertIn("Resync", self.source.drift_summary)

    def test_a_source_that_cannot_be_read_is_not_reported_as_in_sync(self):
        """Unreachable and unchanged must not look the same."""
        KnowledgeSource.objects.filter(pk=self.source.pk).update(fingerprint="abc123")
        self.source.refresh_from_db()
        with self.signalling(""):
            self.assertEqual(check(self.source, self.app), KnowledgeSource.Status.UNREACHABLE)

    def test_detecting_drift_downloads_nothing_and_changes_no_document(self):
        """The whole reason checking can be automatic is that it never acts."""
        Document.objects.create(
            application=self.app,
            uploaded_by=self.owner,
            name="f.md",
            size=1,
            sha256="1" * 64,
            status="ready",
            origin="github",
            source_url="https://raw.example/f.md",
            source=self.source,
        )
        before = list(Document.objects.filter(source=self.source).values_list("status", flat=True))
        with self.signalling("moved"), patch("platform_core.link_sources.download") as download:
            check(self.source, self.app)
        download.assert_not_called()
        after = list(Document.objects.filter(source=self.source).values_list("status", flat=True))
        self.assertEqual(before, after)


@settings_for_tests
class ScheduledCheckTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.source = register(self.owner, self.app, "github", TREE)

    def test_a_source_that_has_never_been_checked_is_due(self):
        with patch("platform_core.knowledge_sources.signal", return_value="x"):
            self.assertTrue(process_next_source())
        self.source.refresh_from_db()
        self.assertIsNotNone(self.source.last_checked_at)

    def test_a_source_checked_recently_is_left_alone(self):
        KnowledgeSource.objects.filter(pk=self.source.pk).update(last_checked_at=timezone.now())
        self.assertFalse(process_next_source())

    def test_a_source_checked_long_ago_comes_round_again(self):
        KnowledgeSource.objects.filter(pk=self.source.pk).update(
            last_checked_at=timezone.now() - timedelta(hours=3)
        )
        with patch("platform_core.knowledge_sources.signal", return_value="x"):
            self.assertTrue(process_next_source())

    def test_a_check_that_raises_does_not_stop_the_lane(self):
        with patch("platform_core.knowledge_sources.signal", side_effect=RuntimeError("boom")):
            self.assertTrue(process_next_source())
        self.source.refresh_from_db()
        self.assertEqual(self.source.status, KnowledgeSource.Status.UNREACHABLE)
        self.assertIsNotNone(self.source.last_checked_at)


@settings_for_tests
class ResyncTests(TestCase):
    """The half that acts, and only ever because somebody asked."""

    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.source = register(self.owner, self.app, "github", TREE)
        from platform_core.link_sources import submit

        with self.resolving([("docs/f0.md", FILE_ZERO), ("docs/f1.md", FILE_ONE)]):
            submit(self.owner, self.app.pk, TREE, source=self.source)

    def resolving(self, files):
        return patch("platform_core.link_sources.github_plan", return_value=files)

    def unchanged_signal(self):
        return patch("platform_core.knowledge_sources.signal", return_value="x")

    def test_a_new_file_upstream_is_queued(self):
        files = [
            ("docs/f0.md", FILE_ZERO),
            ("docs/f1.md", FILE_ONE),
            ("docs/f2.md", "https://raw.example/2.md"),
        ]
        with self.resolving(files), self.unchanged_signal():
            queued, orphaned = resync(self.owner, self.app.pk, self.source.pk)
        self.assertEqual((queued, orphaned), (1, 0))

    def test_a_source_already_up_to_date_is_not_an_error(self):
        """Pressing Resync on an unchanged source answers, rather than failing."""
        with self.resolving([("docs/f0.md", FILE_ZERO), ("docs/f1.md", FILE_ONE)]):
            with self.unchanged_signal():
                queued, orphaned = resync(self.owner, self.app.pk, self.source.pk)
        self.assertEqual((queued, orphaned), (0, 0))

    def test_a_file_gone_from_the_origin_is_marked_not_deleted(self):
        """A rename upstream must not destroy a document a graph version cites."""
        with self.resolving([("docs/f0.md", FILE_ZERO)]), self.unchanged_signal():
            _queued, orphaned = resync(self.owner, self.app.pk, self.source.pk)
        self.assertEqual(orphaned, 1)
        gone = Document.objects.get(source_url=FILE_ONE)
        self.assertTrue(gone.orphaned)
        self.assertNotEqual(gone.status, "deleted")

    def test_a_file_that_comes_back_stops_being_marked(self):
        with self.resolving([("docs/f0.md", FILE_ZERO)]), self.unchanged_signal():
            resync(self.owner, self.app.pk, self.source.pk)
        both = [("docs/f0.md", FILE_ZERO), ("docs/f1.md", FILE_ONE)]
        with self.resolving(both), self.unchanged_signal():
            resync(self.owner, self.app.pk, self.source.pk)
        self.assertFalse(Document.objects.get(source_url=FILE_ONE).orphaned)

    def test_a_viewer_cannot_resync(self):
        with self.assertRaises(PermissionDenied):
            resync(self.viewer, self.app.pk, self.source.pk)

    def test_resyncing_marks_the_source_in_sync_again(self):
        KnowledgeSource.objects.filter(pk=self.source.pk).update(
            status=KnowledgeSource.Status.STALE, fingerprint="old"
        )
        files = [("docs/f0.md", FILE_ZERO), ("docs/f1.md", FILE_ONE)]
        with self.resolving(files):
            with patch("platform_core.knowledge_sources.signal", return_value="new"):
                resync(self.owner, self.app.pk, self.source.pk)
        self.source.refresh_from_db()
        self.assertEqual(self.source.status, KnowledgeSource.Status.READY)
        self.assertEqual(self.source.fingerprint, "new")
        self.assertIsNotNone(self.source.last_synced_at)

    def test_the_button_is_a_post_and_a_viewer_is_refused(self):
        url = reverse("source-resync", args=[self.app.pk, self.source.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        self.client.force_login(self.viewer, backend="django.contrib.auth.backends.ModelBackend")
        self.assertEqual(self.client.post(url).status_code, 403)

    def test_the_source_and_its_button_appear_on_the_page(self):
        body = self.client.get(reverse("knowledge", args=[self.app.pk])).content.decode()
        self.assertIn("Where these came from", body)
        self.assertIn(reverse("source-resync", args=[self.app.pk, self.source.pk]), body)


@settings_for_tests
class SourceDisclosureTests(TestCase):
    """Each origin opens to show what came from it."""

    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.source = register(self.owner, self.app, "github", TREE)
        from platform_core.link_sources import submit

        files = [("docs/f0.md", FILE_ZERO), ("docs/f1.md", FILE_ONE)]
        with patch("platform_core.link_sources.github_plan", return_value=files):
            submit(self.owner, self.app.pk, TREE, source=self.source)

    def body(self):
        return self.client.get(reverse("knowledge", args=[self.app.pk])).content.decode()

    def test_an_origin_is_a_disclosure_that_works_without_javascript(self):
        """A native details element, like every other disclosure on the site."""
        body = self.body()
        self.assertIn('<details class="origin', body)
        self.assertIn("<summary", body.split('<details class="origin')[1][:400])

    def test_it_is_closed_until_somebody_opens_it(self):
        """What have I got is asked far more often than what is in this one."""
        panel = self.body().split('<details class="origin')[1][:200]
        self.assertNotIn(" open", panel)

    def test_opening_it_lists_the_files_that_came_from_it(self):
        body = self.body()
        listing = body.split('class="origin-files"')[1].split("</ul>")[0]
        self.assertIn("docs/f0.md", listing)
        self.assertIn("docs/f1.md", listing)

    def test_a_file_no_longer_at_the_origin_is_marked_in_the_listing(self):
        Document.objects.filter(source_url=FILE_ONE).update(orphaned=True)
        listing = self.body().split('class="origin-files"')[1].split("</ul>")[0]
        self.assertIn("No longer at origin", listing)
        self.assertIn("is-orphaned", listing)

    def test_the_resync_button_is_inside_the_disclosure(self):
        """Nothing acts from a row someone has not opened and looked at."""
        body = self.body()
        opened = body.split('class="origin-body"')[1].split("</details>")[0]
        self.assertIn(reverse("source-resync", args=[self.app.pk, self.source.pk]), opened)

    def test_a_viewer_sees_the_files_but_no_resync(self):
        self.client.force_login(self.viewer, backend="django.contrib.auth.backends.ModelBackend")
        body = self.body()
        self.assertIn("docs/f0.md", body)
        self.assertNotIn(reverse("source-resync", args=[self.app.pk, self.source.pk]), body)
