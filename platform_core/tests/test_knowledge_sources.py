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
        self.assertIn('<div class="origin origin-', body)
        self.assertIn("<details><summary", body)

    def test_it_is_closed_until_somebody_opens_it(self):
        """What have I got is asked far more often than what is in this one."""
        panel = self.body().split("<details>")[1][:200]
        self.assertNotIn(" open", panel)

    def test_opening_it_lists_the_files_that_came_from_it(self):
        body = self.body()
        listing = body.split("origin-table")[1].split("</table>")[0]
        self.assertIn("docs/f0.md", listing)
        self.assertIn("docs/f1.md", listing)

    def test_a_file_no_longer_at_the_origin_is_marked_in_the_listing(self):
        Document.objects.filter(source_url=FILE_ONE).update(orphaned=True)
        listing = self.body().split("origin-table")[1].split("</table>")[0]
        self.assertIn("No longer at origin", listing)
        self.assertIn("is-orphaned", listing)

    def test_the_controls_sit_on_the_summary_line(self):
        """Reachable without opening the origin first, by request.

        They are siblings of the details rather than children of the summary: a
        summary is already a button, and nesting a button inside one is neither
        valid nor operable by keyboard in the way either promises.
        """
        body = self.body()
        tools = body.split('class="origin-tools"')[1].split("</div>")[0]
        self.assertIn(reverse("source-resync", args=[self.app.pk, self.source.pk]), tools)
        self.assertIn(self.source.url, tools)
        # Nothing interactive inside the summary itself.
        summary = body.split("<summary")[1].split("</summary>")[0]
        for tag in ("<button", "<form", "<a "):
            self.assertNotIn(tag, summary)

    def test_a_viewer_sees_the_files_but_no_resync(self):
        self.client.force_login(self.viewer, backend="django.contrib.auth.backends.ModelBackend")
        body = self.body()
        self.assertIn("docs/f0.md", body)
        self.assertNotIn(reverse("source-resync", args=[self.app.pk, self.source.pk]), body)


@settings_for_tests
class NoDuplicationTests(TestCase):
    """A file appears once: under its origin, or in the table, never both."""

    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.source = register(self.owner, self.app, "github", TREE)
        from platform_core.link_sources import submit

        with patch(
            "platform_core.link_sources.github_plan",
            return_value=[("docs/imported.md", FILE_ZERO)],
        ):
            submit(self.owner, self.app.pk, TREE, source=self.source)
        self.uploaded = Document.objects.create(
            application=self.app,
            uploaded_by=self.owner,
            name="uploaded-by-hand.md",
            size=1,
            sha256="a" * 64,
            status="ready",
            origin="upload",
        )

    def page(self, query=None):
        url = reverse("knowledge", args=[self.app.pk])
        return self.client.get(f"{url}?q={query}" if query else url).content.decode()

    def table(self, body):
        return body.split('class="table-wrap sources-table"')[1]

    def test_an_imported_file_is_listed_under_its_origin_only(self):
        body = self.page()
        self.assertIn("docs/imported.md", body.split("origin-table")[1])
        self.assertNotIn("docs/imported.md", self.table(body))

    def test_an_upload_is_listed_in_the_table_only(self):
        """It came from nowhere, so it belongs to no origin."""
        body = self.page()
        self.assertIn("uploaded-by-hand.md", self.table(body))
        listing = body.split("origin-table")[1].split("</table>")[0]
        self.assertNotIn("uploaded-by-hand.md", listing)

    def test_searching_finds_a_file_wherever_it_lives(self):
        """Grouping the answer would hide matches inside unopened sources."""
        body = self.page(query="imported")
        self.assertIn("docs/imported.md", self.table(body))

    def test_searching_puts_the_origins_panel_away(self):
        """One list at a time: the grouped view is not the shape of an answer."""
        self.assertIn("Where these came from", self.page())
        self.assertNotIn("Where these came from", self.page(query="imported"))

    def test_the_table_says_what_it_is_holding(self):
        self.assertIn("Everything else", self.page())
        self.assertIn("Search results", self.page(query="imported"))

    def test_the_total_still_counts_everything_after_the_split(self):
        """Splitting the page must not make the total shrink.

        Two documents, one under an origin and one not, and the count beside the
        tab is still two - it counts the application's sources, not whichever
        list happens to be on screen.
        """
        self.assertIn('class="tab-count"', self.page())
        self.assertIn(">2</span>", self.page())


@settings_for_tests
class SourceDeleteTests(TestCase):
    """Removing an origin, and deciding what happens to what it brought in."""

    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.source = register(self.owner, self.app, "github", TREE)
        from platform_core.link_sources import submit

        with patch(
            "platform_core.link_sources.github_plan",
            return_value=[("docs/f0.md", FILE_ZERO), ("docs/f1.md", FILE_ONE)],
        ):
            submit(self.owner, self.app.pk, TREE, source=self.source)

    def url(self):
        return reverse("source-delete", args=[self.app.pk, self.source.pk])

    def live(self):
        return Document.objects.filter(application=self.app).exclude(status="deleted").count()

    def test_it_asks_before_it_does_anything(self):
        """A GET is the dialog, not the deletion."""
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(KnowledgeSource.objects.filter(pk=self.source.pk).exists())
        self.assertEqual(self.live(), 2)

    def test_the_dialog_names_both_outcomes_and_the_count(self):
        """Which one "delete a source" means is not guessable, so it is asked."""
        body = self.client.get(self.url()).content.decode()
        self.assertIn("Remove source, keep documents", body)
        self.assertIn("Remove source and 2 documents", body)

    def test_keeping_the_documents_leaves_them_without_an_origin(self):
        self.client.post(self.url(), {"documents": "keep"})
        self.assertFalse(KnowledgeSource.objects.filter(pk=self.source.pk).exists())
        self.assertEqual(self.live(), 2)
        self.assertEqual(Document.objects.filter(source__isnull=True).count(), 2)

    def test_kept_documents_reappear_under_everything_else(self):
        self.client.post(self.url(), {"documents": "keep"})
        body = self.client.get(reverse("knowledge", args=[self.app.pk])).content.decode()
        self.assertIn("docs/f0.md", body.split("sources-table")[1])

    def test_removing_them_marks_them_deleted_rather_than_erasing_them(self):
        """The same path the single-document delete uses, so history survives."""
        self.client.post(self.url(), {"documents": "delete"})
        self.assertFalse(KnowledgeSource.objects.filter(pk=self.source.pk).exists())
        self.assertEqual(self.live(), 0)
        self.assertEqual(Document.objects.filter(status="deleted").count(), 2)

    def test_anything_other_than_delete_is_read_as_keep(self):
        """The destructive reading is never the default of an unclear request."""
        self.client.post(self.url(), {})
        self.assertEqual(self.live(), 2)

    def test_a_contributor_cannot_remove_a_source(self):
        """Resyncing is contributor work; removing the origin is the owner's."""
        from platform_core.models import ApplicationGrant

        ApplicationGrant.objects.filter(application=self.app, user=self.owner).update(
            role="contributor"
        )
        self.assertEqual(self.client.post(self.url(), {"documents": "keep"}).status_code, 403)
        self.assertTrue(KnowledgeSource.objects.filter(pk=self.source.pk).exists())

    def test_removal_is_recorded(self):
        from platform_core.models import AuditEvent

        self.client.post(self.url(), {"documents": "keep"})
        self.assertTrue(AuditEvent.objects.filter(action="source.forgotten").exists())

    def test_the_control_opens_a_dialog_rather_than_deleting_on_click(self):
        body = self.client.get(reverse("knowledge", args=[self.app.pk])).content.decode()
        tools = body.split('class="origin-tools"')[1].split("</div>")[0]
        self.assertIn(self.url(), tools)
        self.assertIn("data-modal", tools)


@settings_for_tests
class KnowledgeHeadingTests(TestCase):
    """One name for one section, said once.

    The section description was printed above the tab strip on all four views,
    so the same sentence introduced the graph, the quality report and the
    version list. The heading itself said "Knowledge sources" on one view and
    "Knowledge" on the other three, which made one place look like two.
    """

    def setUp(self):
        test_documents.DocumentTests.setUp(self)

    def views(self):
        app = self.app.pk
        return {
            "sources": self.client.get(reverse("documents", args=[app])),
            "graph": self.client.get(reverse("graph", args=[app])),
            "quality": self.client.get(reverse("graph", args=[app]), {"tab": "quality"}),
            "versions": self.client.get(reverse("graph", args=[app]), {"tab": "versions"}),
        }

    def test_every_view_keeps_a_heading_in_the_outline(self):
        """Off the page, not out of the document.

        The application menu names the section and marks it current, so printing
        "Knowledge" again above the tabs told nobody anything. Somebody moving
        through the page by heading still needs to land somewhere.
        """
        for name, response in self.views().items():
            with self.subTest(view=name):
                self.assertContains(response, '<h1 class="sr-only">Knowledge</h1>')

    def test_the_application_name_is_not_printed_a_third_time(self):
        """It is already in the menu and in the breadcrumb."""
        sources = self.views()["sources"]
        self.assertNotContains(sources, '<p class="eyebrow">')

    def test_each_view_says_what_it_is_for(self):
        """Restored by request, but one line per view rather than one repeated."""
        for name, response in self.views().items():
            with self.subTest(view=name):
                self.assertContains(response, 'class="view-note"')

    def build_graph(self):
        from platform_core.graphs import rebuild
        from platform_core.workbench import add_knowledge

        add_knowledge(
            self.owner, self.app.pk, "Configuration",
            "| Node | Queue |" + chr(10) + "| --- | --- |" + chr(10) + "| NODE1 | Q1 |",
        )
        rebuild(self.app.pk)

    def test_no_two_views_carry_the_same_description(self):
        """The old sentence described the section, so it read the same on all of them."""
        import re

        notes = {}
        for name, response in self.views().items():
            found = re.findall(
                r'<p class="view-note">(.*?)</p>', response.content.decode(), re.S
            )
            if found:
                notes[name] = found[0]
        self.assertEqual(len(notes), len(set(notes.values())))

    def test_the_tab_strip_is_what_says_which_view_this_is(self):
        """Dropping the descriptions is only safe because this still marks it."""
        for name, response in self.views().items():
            with self.subTest(view=name):
                self.assertContains(response, 'aria-current="page"')

    TITLES = {"sources": "Sources", "graph": "Graph", "quality": "Quality",
              "versions": "Versions"}

    def test_every_view_is_named_on_the_page(self):
        """Put back by request after being hidden for space.

        The heading is the tab's own word, so the thing that says which view
        this is and the thing that switched to it cannot drift apart.
        """
        self.build_graph()
        views = self.views()
        for name, title in self.TITLES.items():
            with self.subTest(view=name):
                self.assertContains(views[name], f'<h2 class="view-title">{title}</h2>')
                self.assertNotContains(views[name], f'class="sr-only">{title}')

    def test_no_view_prints_its_own_name_twice(self):
        """Each view used to be headed again by the one thing it contains.

        Versions was headed "Graph versions" over its only table and Quality
        "Graph quality" over its only report, which with the view named above
        them said the same thing twice. Panels inside the graph carry their own
        names and are not this.
        """
        self.build_graph()
        views = self.views()
        for name, title in self.TITLES.items():
            with self.subTest(view=name):
                body = views[name].content.decode()
                self.assertEqual(body.count(f">{title}</h2>"), 1, body.count(f">{title}</h2>"))

    def test_the_view_name_is_not_also_the_table_heading(self):
        """With one list there is no second list to tell it apart from.

        The table used to be headed "Sources" itself, which with the view named
        above it printed the same word twice for one table.
        """
        Document.objects.create(
            application=self.app, uploaded_by=self.owner, name="one.md", size=1,
            sha256="c" * 64, status="ready", origin="upload",
        )
        body = self.client.get(reverse("documents", args=[self.app.pk])).content.decode()
        self.assertEqual(body.count(">Sources</h2>"), 1)

    def test_the_source_count_moves_onto_the_tab_it_belongs_to(self):
        """Removing the heading must not take the number with it."""
        Document.objects.create(
            application=self.app, uploaded_by=self.owner, name="one.md", size=1,
            sha256="b" * 64, status="ready", origin="upload",
        )
        sources = self.client.get(reverse("documents", args=[self.app.pk]))
        self.assertContains(sources, 'class="tab-count"')
        self.assertContains(sources, ">1</span>")

    def test_no_badge_is_shown_when_there_is_nothing_to_count(self):
        """A zero beside a tab reads as a defect rather than as an empty list."""
        self.assertNotContains(self.views()["sources"], 'class="tab-count"')
