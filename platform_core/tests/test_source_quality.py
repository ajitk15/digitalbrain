"""Each source is checked for what the graph can make of it, and says so."""

import hashlib
import json
import uuid
from io import StringIO
from types import SimpleNamespace

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from platform_core import source_quality
from platform_core.graphs import build_graph, walk
from platform_core.models import Document, KnowledgeEntry
from platform_core.source_quality import assess
from platform_core.workbench import add_knowledge

from . import test_documents
from .test_ticket_quality import SETTINGS

GOOD = """# Referral policy

Document: POL-CAREPATH-007 · Version 1.2 · Owner: Integrated Care · 2026-03-01

## Rules

| Rule | Applies to | Owner |
| --- | --- | --- |
| R-01 | Every referral | Coordinator |
| R-02 | Urgent referrals | Clinical lead |

Every referral is acknowledged within one working day.
"""

#: Two tables the graph reads badly: one with a blank header and nothing to put
#: in its place, and one a PDF broke so only its header is left.
FLAWED = """Release plan

|  |  |
| --- | --- |
| Step | Owner |
| Freeze | Delivery |

| ID | Obligation |
| --- | --- |
"""


#: How MarkItDown writes a Word table whose header row is not marked as one.
BOLD_HEADER = """|  |  |
| --- | --- |
| **Step** | **Owner** |
| Freeze | Delivery |
"""


def check(content, key, report=None):
    return next(c for c in assess(content, report)["checks"] if c.key == key)


class AssessTests(SimpleTestCase):
    def test_a_complete_document_is_ready(self):
        result = assess(GOOD)
        self.assertEqual(result["gaps"], [], result["gaps"])
        self.assertEqual((result["score"], result["verdict"], result["tone"]),
                         (100, "Ready", "good"))

    def test_every_gap_says_what_was_found_and_how_to_fix_it(self):
        result = assess(FLAWED)
        self.assertNotEqual(result["verdict"], "Ready")
        for gap in result["gaps"]:
            self.assertTrue(gap.finding and gap.improve and gap.rule, gap.key)
        # The largest gain comes first.
        self.assertEqual(result["gaps"][0].key, "tables")

    def test_table_findings_name_their_lines(self):
        tables = check(FLAWED, "tables")
        self.assertEqual(tables.state, "missing")
        self.assertIn("1 with a blank header row (line 3)", tables.finding)
        self.assertIn("1 with a header and no rows (line 8)", tables.finding)
        self.assertIn("Header Row", tables.improve)
        self.assertIn("Word or Excel original", tables.improve)

    def test_a_bold_first_row_under_a_blank_header_is_the_header(self):
        """How MarkItDown writes a Word table whose header row is not marked as one."""
        self.assertEqual(check(BOLD_HEADER, "tables").state, "good")
        [table] = source_quality.tables_in(BOLD_HEADER)
        self.assertEqual((table["line"], table["headers"], table["rows"]),
                         (3, ["Step", "Owner"], [["Freeze", "Delivery"]]))

    def test_a_front_matter_table_is_not_a_blank_header(self):
        """`| **Version** | 1.2 |` rows name themselves; there is no column to name."""
        content = "|  |  |\n| --- | --- |\n| **Document** | DOC-ABC-001 |\n| **Version** | 1.2 |\n"
        self.assertEqual(check(content, "tables").state, "good")

    def test_placeholder_column_names_are_found(self):
        content = "| Unnamed: 0 | Name |\n| --- | --- |\n| 1 | Alpha |\n"
        self.assertIn("placeholder column names", check(content, "tables").finding)

    def test_repeated_rows_and_empty_cells_are_found(self):
        content = "| A | B | C |\n| --- | --- | --- |\n| x |  |  |\n| x |  |  |\n"
        finding = check(content, "tables").finding
        self.assertIn("1 with repeated rows (line 1)", finding)
        self.assertIn("67% of table cells are empty", finding)

    def test_long_text_with_no_headings_needs_structure(self):
        prose = "\n".join(f"Step {n}: restart the service and check it." for n in range(20))
        structure = check(prose, "structure")
        self.assertEqual((structure.state, structure.earned), ("missing", 0))
        self.assertIn("20 lines of text and no headings", structure.finding)
        # A short note needs none.
        self.assertEqual(check("One line.\nTwo lines.", "structure").state, "good")

    def test_identity_is_read_from_the_opening(self):
        self.assertEqual(check(GOOD, "identity").state, "good")
        dated = check("Board pack\nClinical Systems Board, 2026-06-02\n" + "text\n" * 5,
                      "identity")
        self.assertEqual((dated.state, dated.earned), ("partial", 8))
        # A requirement the document cites is not its document number.
        cites = check("Notes on FR-01 and BR-05.\n", "identity")
        self.assertEqual(cites.state, "missing")

    def test_open_placeholders_are_quoted_with_their_line(self):
        finished = check(GOOD + "\nRetention period: TBD\n", "finished")
        self.assertEqual(finished.state, "partial")
        self.assertIn("line 14", finished.finding)
        self.assertIn("TBD", finished.finding)

    def test_text_too_short_or_garbled_is_not_readable(self):
        self.assertEqual(check("Scanned.", "readable").state, "missing")
        garbled = check(GOOD + "�", "readable")
        self.assertEqual(garbled.state, "partial")
        self.assertIn("1 character could not be decoded", garbled.finding)

    def test_pages_with_no_text_come_from_the_conversion_report(self):
        report = {"pages": 4, "pages_without_text": [2, 3, 4]}
        readable = check(GOOD, "readable", report)
        self.assertEqual(readable.state, "missing")
        self.assertIn("3 pages of 4 had no text (pages 2, 3, 4)", readable.finding)
        self.assertIn("OCR", readable.improve)

    def test_more_than_the_graph_reads_is_said(self):
        rows = "\n".join(f"| {n} | value |" for n in range(2100))
        limits = check(f"| N | V |\n| --- | --- |\n{rows}\n", "limits")
        self.assertEqual(limits.state, "partial")
        self.assertIn("the graph reads the first 2,000", limits.finding)

    def test_ready_needs_no_check_missing(self):
        checks = [source_quality.Check("a", "A", 85, 85, "good", "", "", ""),
                  source_quality.Check("b", "B", 15, 0, "missing", "", "", "")]
        self.assertEqual(source_quality.verdict(85, checks), "Needs work")
        self.assertEqual(source_quality.verdict(85, checks[:1]), "Ready")

    def test_a_high_score_marked_needs_work_names_the_check_that_failed(self):
        """85 and Needs work reads as a contradiction until it says which check."""
        checks = [source_quality.Check("a", "Readable text", 85, 85, "good", "Fine.", "", ""),
                  source_quality.Check("b", "Structure", 15, 0, "missing",
                                       "19 lines and no heading.", "Add headings.", "")]
        shown = source_quality.scored(checks)
        self.assertEqual(shown["verdict"], "Needs work")
        self.assertEqual(shown["decisive"].label, "Structure")
        self.assertEqual(shown["fix"], "Add headings")
        self.assertTrue(shown["why"].startswith("Fix first: Add headings. It scored 85/100"))
        self.assertIn("and “Structure” failed.", shown["why"])
        self.assertIn("Ready: 80/100 or more", shown["rule"])

    def test_a_low_score_names_where_most_points_were_lost(self):
        checks = [source_quality.Check("a", "Readable text", 50, 40, "partial", "Thin.", "", ""),
                  source_quality.Check("b", "Structure", 50, 20, "partial", "Few headings.", "",
                                       "")]
        shown = source_quality.scored(checks)
        self.assertEqual(shown["verdict"], "Needs work")
        self.assertEqual(shown["decisive"].label, "Structure")
        self.assertIn("Ready needs 80/100; it scored 60", shown["why"])
        self.assertIn("lost the most on “Structure” (-30).", shown["why"])
        # No advice to give, so the fix is what was found.
        self.assertEqual(shown["fix"], "Few headings")

    def test_ready_says_so_and_names_no_check(self):
        checks = [source_quality.Check("a", "A", 100, 100, "good", "", "", "")]
        shown = source_quality.scored(checks)
        self.assertIsNone(shown["decisive"])
        self.assertEqual(shown["why"], "Ready: 100/100 and no check failed.")
        self.assertEqual(shown["fix"], "")

    def test_the_check_reads_the_rows_the_graph_reads(self):
        """One walk for both, so a table finding cannot disagree with the graph."""
        content = GOOD + "\n" + FLAWED
        entry = SimpleNamespace(pk=uuid.uuid4(), application_id=uuid.uuid4(), title="t",
                                content=content, digest="d")
        _, quality = build_graph([entry], 1)
        rows = sum(len(t["rows"]) for t in source_quality.tables_in(content))
        self.assertEqual(rows, quality["table_rows"])


class WalkTests(SimpleTestCase):
    #: Tables, separators with alignment, a table line with no header above it,
    #: a repeated row, an empty cell, headings and prose.
    SAMPLE = """# Title

| A | B |
| :-- | --: |
| one | 1 |
| one | 1 |
| two |  |
Prose straight after a table.
| orphan | line |
## Heading

| C | D | E |
| --- | --- | --- |
| x | y \\| z | w |
"""

    def test_the_walk_yields_headers_rows_and_sections(self):
        kinds = [(item.kind, item.index) for item in walk(self.SAMPLE)]
        self.assertEqual(kinds, [
            ("section", 0), ("header", 2), ("row", 4), ("row", 5), ("row", 6),
            ("section", 7), ("section", 9), ("header", 11), ("row", 13),
        ])

    def test_a_bold_row_under_a_blank_header_names_the_columns_in_the_graph(self):
        entry = SimpleNamespace(pk=uuid.uuid4(), application_id=uuid.uuid4(), title="t",
                                content=BOLD_HEADER, digest="d")
        data, quality = build_graph([entry], 1)
        self.assertEqual(quality["table_rows"], 1)
        fields = sorted(node["field"] for node in data["nodes"] if node["kind"] == "value")
        self.assertEqual(fields, ["Owner", "Step"])

    def test_only_a_wholly_bold_row_is_promoted(self):
        """Front matter (`| **Version** | 1.2 |`) and a row with a gap stay rows."""
        for first in ("| **Version** | 1.2 |", "| **Step** |  |", "| Step | Owner |"):
            with self.subTest(first=first):
                content = "\n".join(["|  |  |", "| --- | --- |", first, "| a | b |"])
                kinds = [item.kind for item in walk(content)]
                self.assertEqual(kinds, ["header", "row", "row"])

    def test_the_graph_is_unchanged_by_moving_the_walk_out_of_it(self):
        """Pinned from the builder before `walk` existed: same text, same graph."""
        entry = SimpleNamespace(pk=uuid.UUID(int=7), application_id=uuid.UUID(int=1),
                                title="Sample", content=self.SAMPLE, digest="d" * 64)
        graph, _ = build_graph([entry], 1)
        digest = hashlib.sha256(json.dumps(graph, sort_keys=True).encode()).hexdigest()
        self.assertEqual(digest, PINNED_GRAPH)


#: The graph build_graph(WalkTests.SAMPLE) made at 9e7dff0, before the walk was
#: factored out. The sample has no blank header, so promoting a bold row leaves it be.
PINNED_GRAPH = "03c3d28807f2b4b3be06db95e896b08e0d4b763221ee43446b9b9774490bcfef"


@override_settings(**SETTINGS)
class SourceCheckScreenTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)

    def document(self, content, name="plan.docx"):
        document = Document.objects.create(
            application=self.app, uploaded_by=self.owner, name=name, size=len(content),
            sha256=hashlib.sha256(content.encode()).hexdigest(), status="ready",
            origin="upload",
        )
        entry = add_knowledge(self.owner, self.app.pk, name, content, document=document)
        return document, entry

    def test_adding_a_source_stores_its_check(self):
        _, entry = self.document(FLAWED)
        entry.refresh_from_db()
        self.assertEqual(entry.quality["version"], source_quality.QUALITY_VERSION)
        self.assertEqual([c["key"] for c in entry.quality["checks"]],
                         ["readable", "tables", "structure", "identity", "finished", "limits"])

    def test_a_typed_record_is_not_a_document_and_is_not_checked(self):
        entry = add_knowledge(self.owner, self.app.pk, "Note", "Refunds expire after 30 days.")
        self.assertEqual(entry.quality, {})
        response = self.client.get(reverse("knowledge-detail", args=[self.app.pk, entry.pk]))
        self.assertNotContains(response, 'id="source-check"')

    def test_the_sources_table_marks_what_needs_work_and_links_to_why(self):
        document, _ = self.document(FLAWED)
        self.document(GOOD, name="policy.md")
        response = self.client.get(reverse("documents", args=[self.app.pk]))
        detail = reverse("document-detail", args=[self.app.pk, document.pk])
        self.assertContains(response, f'href="{detail}#source-check"')
        # Every checked source says its verdict, by name, in the Quality
        # column; the one that is not Ready says which check decided it.
        self.assertContains(response, '<th>Quality<small class="th-note">Format: can the graph')
        self.assertContains(response, '<span class="check-name">Format</span>', count=2)
        self.assertContains(response, '<span class="check-verdict">ready</span>')
        self.assertContains(response, '<span class="check-verdict">poor</span>')
        self.assertContains(response, '<small class="check-fix">Fix: In Word, mark each table')
        # The score has its scale, and the rule that turns it into a verdict is said.
        self.assertContains(response, '/100</span>')
        self.assertContains(response, 'class="check-legend">Ready: 80/100 or more')

    def test_the_document_page_explains_the_check(self):
        document, _ = self.document(FLAWED)
        response = self.client.get(reverse("document-detail", args=[self.app.pk, document.pk]))
        self.assertContains(response, '<details class="card ticket-quality" id="source-check">')
        self.assertContains(response, "Format check")
        self.assertContains(response, "Can the graph read the text, tables and headings?")
        self.assertContains(response, "Poor · ")
        self.assertContains(response, "things to fix")
        self.assertContains(response, "nothing stops a graph being built")

    def test_the_document_page_says_why_on_the_cards_own_line(self):
        document, _ = self.document(FLAWED)
        response = self.client.get(reverse("document-detail", args=[self.app.pk, document.pk]))
        self.assertContains(response, '<small class="ticket-quality-why">Fix first: In Word,')
        self.assertContains(response, "How the verdict is decided.")
        self.assertContains(response, "Here it was decided by <strong>Tables the graph can read")

    def test_the_sources_table_has_a_document_type_and_a_file_type(self):
        self.document(GOOD, name="policies/referral.docx")
        self.document(FLAWED, name="export.xlsx")
        response = self.client.get(reverse("documents", args=[self.app.pk]))
        self.assertContains(response, "<th>Document type</th>", html=False)
        self.assertContains(response, "<th>File type</th>", html=False)
        self.assertContains(
            response, '<span class="file-type file-docx" title="DOCX file">Word</span>'
        )
        self.assertContains(
            response, '<span class="file-type file-xlsx" title="XLSX file">Excel</span>'
        )

    def test_delete_and_retry_are_icons_that_still_say_what_they_do(self):
        document, _ = self.document(GOOD)
        Document.objects.filter(pk=document.pk).update(status="failed")
        response = self.client.get(reverse("documents", args=[self.app.pk]))
        self.assertContains(response, 'aria-label="Retry converting plan.docx"')
        self.assertContains(response, 'aria-label="Delete plan.docx"')
        self.assertContains(response, reverse("document-retry", args=[self.app.pk, document.pk]))
        self.assertNotContains(response, ">Retry</button>")
        self.assertNotContains(response, ">Delete</a>")

    def test_the_record_page_shows_the_check_and_a_viewer_sees_it(self):
        _, entry = self.document(FLAWED)
        self.client.force_login(self.viewer, backend="django.contrib.auth.backends.ModelBackend")
        response = self.client.get(reverse("knowledge-detail", args=[self.app.pk, entry.pk]))
        self.assertContains(response, 'id="source-check"')
        self.assertNotContains(response, 'id="ticket-quality"')

    def test_a_restricted_source_is_still_checked(self):
        """People read a restricted source; only models do not."""
        _, entry = self.document(FLAWED)
        KnowledgeEntry.objects.filter(pk=entry.pk).update(sensitivity="restricted")
        response = self.client.get(reverse("knowledge-detail", args=[self.app.pk, entry.pk]))
        self.assertContains(response, 'id="source-check"')

    def test_a_check_under_older_rules_is_worked_out_again_and_kept(self):
        document, entry = self.document(FLAWED)
        KnowledgeEntry.objects.filter(pk=entry.pk).update(quality={"version": 0, "checks": []})
        response = self.client.get(reverse("document-detail", args=[self.app.pk, document.pk]))
        self.assertContains(response, 'id="source-check"')
        entry.refresh_from_db()
        self.assertEqual(entry.quality["version"], source_quality.QUALITY_VERSION)

    def test_check_sources_fills_existing_rows(self):
        _, entry = self.document(FLAWED)
        KnowledgeEntry.objects.filter(pk=entry.pk).update(quality={})
        out = StringIO()
        call_command("check_sources", "--application", str(self.app.pk), stdout=out)
        self.assertIn("Checked 1 source(s): 0 ready, 0 needs work, 1 poor.", out.getvalue())
        entry.refresh_from_db()
        self.assertEqual(entry.quality["version"], source_quality.QUALITY_VERSION)
