"""The sources are read together: what they define, cite and share."""

import hashlib
import uuid
from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from platform_core import source_map
from platform_core.models import Document, KnowledgeEntry
from platform_core.source_map import analyse, defined_in, references, spans
from platform_core.workbench import add_knowledge

from . import test_documents
from .test_source_quality import FLAWED
from .test_ticket_quality import SETTINGS

REQUIREMENTS = """# Requirements

Document: REQ-CARE-001 · Version 1.0 · Owner: Product · 2026-01-01

| ID | Requirement | Status |
| --- | --- | --- |
| BR-01 | Every referral is acknowledged | Approved |
| BR-02 | Consent is checked before export | Approved |
| BR-03 | Risk is scored on admission | Approved |
"""

TESTS = """# Test plan

Document: TST-CARE-001 · Version 1.0 · Owner: QA · 2026-01-02

| Case | Covers | Status |
| --- | --- | --- |
| Acknowledge a referral | BR-01 | Approved |
| Export with consent | BR-02 | Approved |

The export case also exercises F-01, which no document defines.
"""

RECIPE = """Lemon drizzle cake. Cream the butter and sugar until pale, then beat in the
eggs one at a time. Fold in the flour and the zest of two lemons, bake for forty
minutes, then pour over a syrup of lemon juice and icing sugar while it is warm."""


def source(content, title, number):
    return SimpleNamespace(pk=uuid.UUID(int=number), title=title, content=content,
                           digest=hashlib.sha256(content.encode()).hexdigest())


class AnalyseTests(SimpleTestCase):
    def setUp(self):
        self.result = analyse([
            source(REQUIREMENTS, "requirements", 1),
            source(TESTS, "test plan", 2),
            source(RECIPE, "cake", 3),
        ])
        self.rows = {row["entry"].title: row for row in self.result["sources"]}

    def test_a_reference_is_defined_where_it_starts_a_row_and_mentioned_elsewhere(self):
        self.assertEqual(defined_in(REQUIREMENTS), {"BR-01", "BR-02", "BR-03"})
        self.assertEqual(defined_in(TESTS), set())
        self.assertEqual(self.rows["requirements"]["cited_by"], 1)
        self.assertEqual(self.rows["test plan"]["cites"], 1)

    def test_a_heading_or_a_numbered_list_defines_too(self):
        self.assertEqual(defined_in("## NFR-06 Availability\nText."), {"NFR-06"})
        listed = "R-01 Data loss\nR-02 Outage\nR-03 Breach\nSee R-09 later."
        self.assertEqual(defined_in(listed), {"R-01", "R-02", "R-03"})

    def test_standards_are_not_references(self):
        self.assertEqual(list(references("ISO-8601, SHA-256, SEV-2 and BR-05")), ["BR-05"])

    def test_what_nothing_else_names_is_listed_as_a_reader_would_write_it(self):
        self.assertEqual(self.result["unreferenced"],
                         [{"prefix": "BR", "count": 1, "spans": ["BR-03"]}])
        self.assertEqual(self.result["missing"], ["F-01"])
        self.assertEqual(spans(["US-03", "US-01", "US-02", "R-04", "R-06", "R-07"]),
                         ["R-04", "R-06 and R-07", "US-01 to US-03"])

    def test_a_source_about_something_else_stands_alone(self):
        self.assertEqual([row["entry"].title for row in self.result["isolated"]], ["cake"])
        self.assertEqual([row["entry"].title for row in self.result["off_topic"]], ["cake"])
        self.assertEqual(self.result["connected"], 2)

    def test_a_shared_table_value_is_a_join_in_the_graph(self):
        """Both tables hold Status: Approved, so the graph has one node for it."""
        self.assertEqual(self.rows["requirements"]["shares"], 1)
        self.assertEqual(self.result["by_value"], 2)

    def test_the_same_document_twice_is_found(self):
        twice = analyse([source(REQUIREMENTS, "a", 1), source(REQUIREMENTS, "b", 2)])
        self.assertEqual(len(twice["duplicates"]), 1)
        self.assertTrue(twice["duplicates"][0]["identical"])

    def test_one_source_is_neither_isolated_nor_off_topic(self):
        alone = analyse([source(RECIPE, "cake", 1)])
        self.assertEqual((alone["isolated"], alone["off_topic"]), ([], []))


@override_settings(**SETTINGS)
class SourceCheckPageTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        source_map._cache.clear()

    def document(self, content, name, app=None, user=None):
        app, user = app or self.app, user or self.owner
        document = Document.objects.create(
            application=app, uploaded_by=user, name=name, size=len(content),
            sha256=hashlib.sha256(content.encode()).hexdigest(), status="ready",
            origin="upload",
        )
        return add_knowledge(user, app.pk, name, content, document=document)

    def page(self):
        return self.client.get(reverse("source-check", args=[self.app.pk]))

    def test_the_page_reads_the_set_and_links_each_source(self):
        entry = self.document(REQUIREMENTS, "requirements.docx")
        self.document(TESTS, "test-plan.docx")
        self.document(FLAWED, "release-plan.docx")
        response = self.page()
        self.assertContains(response, "Check sources")
        self.assertContains(response, "Connect to another source")
        self.assertContains(response, "<strong>2 of 3</strong>", html=False)
        self.assertContains(response, "Not referenced by any other source")
        self.assertContains(response, "BR-03")
        self.assertContains(response, "Mentioned, not found")
        # Each source's two checks, in the words and lines the Sources list uses.
        self.assertContains(response, '<span class="check-name">Format</span>', count=3)
        self.assertContains(response, '<span class="check-name">Content</span>', count=3)
        self.assertContains(response, '<span class="check-verdict">poor</span>')
        self.assertContains(response, 'class="check-legend">Ready: 80/100 or more')
        self.assertContains(response, "F-01")
        self.assertContains(
            response,
            reverse("document-detail", args=[self.app.pk, entry.document_id]) + "#source-check",
        )
        # A viewer reads it too: it says what the sources are, not what to do.
        self.client.force_login(self.viewer, backend="django.contrib.auth.backends.ModelBackend")
        self.assertEqual(self.page().status_code, 200)

    def test_another_applications_sources_never_appear(self):
        from platform_core.models import ApplicationGrant

        ApplicationGrant.objects.create(application=self.other, user=self.owner, role="owner")
        self.document(REQUIREMENTS, "requirements.docx")
        self.document(TESTS, "foreign-test-plan.docx", app=self.other)
        response = self.page()
        self.assertNotContains(response, "foreign-test-plan.docx")
        self.assertContains(response, "<strong>0 of 1</strong>", html=False)

    def test_a_restricted_source_is_left_out_as_the_graph_leaves_it_out(self):
        self.document(REQUIREMENTS, "requirements.docx")
        hidden = self.document(TESTS, "test-plan.docx")
        KnowledgeEntry.objects.filter(pk=hidden.pk).update(sensitivity="restricted")
        response = self.page()
        self.assertNotContains(response, "test-plan.docx")

    def test_no_access_is_not_found(self):
        self.client.force_login(self.admin, backend="django.contrib.auth.backends.ModelBackend")
        self.assertEqual(self.page().status_code, 404)

    def test_sources_says_it_in_one_line_and_links_the_popup(self):
        self.document(REQUIREMENTS, "requirements.docx")
        self.document(FLAWED, "release-plan.docx")
        response = self.client.get(reverse("documents", args=[self.app.pk]))
        self.assertContains(response, '<p class="source-summary">', html=False)
        self.assertContains(response, "<strong>1</strong> ready", html=False)
        self.assertContains(response, "<strong>1</strong> poor", html=False)
        self.assertContains(
            response, f'href="{reverse("source-check", args=[self.app.pk])}" data-modal'
        )

    def test_generate_says_what_needs_attention_and_still_generates(self):
        """Counted in sources: the release plan is poor and stands alone, and is one."""
        self.document(REQUIREMENTS, "requirements.docx")
        self.document(TESTS, "test-plan.docx")
        self.document(FLAWED, "release-plan.docx")
        response = self.client.get(reverse("graph-generate", args=[self.app.pk]))
        self.assertContains(response, "1 source needs attention before this graph is built")
        self.assertContains(response, reverse("source-check", args=[self.app.pk]))
        self.assertContains(response, '<button class="button" type="submit">', html=False)

    def test_the_result_is_kept_until_a_source_changes(self):
        self.document(REQUIREMENTS, "requirements.docx")
        first = source_map.for_application(self.app.pk)
        self.assertIs(source_map.for_application(self.app.pk), first)
        self.document(TESTS, "test-plan.docx")
        self.assertEqual(source_map.for_application(self.app.pk)["count"], 2)
