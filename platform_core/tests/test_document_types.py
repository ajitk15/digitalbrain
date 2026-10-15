"""Each document is recognised as the kind it is, and judged as one."""

import hashlib
from pathlib import Path

from django.db import transaction
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from platform_core import document_types, source_quality
from platform_core.document_types import checks, classify
from platform_core.models import AuditEvent, Document, KnowledgeEntry
from platform_core.processing import supersede
from platform_core.workbench import add_knowledge

from . import test_documents
from .test_ticket_quality import SETTINGS

DOCS = Path(__file__).resolve().parents[2] / "demo-artifacts" / "docs"

#: The repository's own Markdown documents and what a person would call them.
#: The Word, Excel and PDF ones are typed by the scorecard, which converts them.
LABELLED = {
    "02-design/adr/0001-sqlite-for-release-one.md": "adr",
    "02-design/adr/0002-static-token-directory.md": "adr",
    "02-design/adr/0003-transparent-in-process-scoring.md": "adr",
    "02-design/adr/0004-fhir-profile-scope.md": "adr",
    "02-design/data-model.md": "data_model",
    "03-build/coding-standards.md": "standards",
    "05-release/deployment-runbook.md": "runbook",
    "08-quality-samples/escalation-policy.md": "policy",
    "08-quality-samples/staff-canteen-menu.md": "other",
    "08-quality-samples/discharge-summary-template.md": "other",
}

#: A business requirements document named nothing like one, with no document
#: number: only what it contains can say what it is.
BRD = """What the clinics need from CarePath

## Background

Referrals between the hospital and the community clinics are posted on paper, and
nobody can see where a referral is once it has left the ward that raised it.

## Scope

Referral tracking, consent capture and the access report, for the two pilot
clinics, from June until the end of the pilot review.

## Out of scope

Scheduling, prescribing and anything a patient uses directly; those stay where
they are and are revisited after the pilot.

## Requirements

| ID | Requirement | Rationale | Measure of success | Priority |
| --- | --- | --- | --- | --- |
| BR-01 | Referrals are tracked to closure | Nobody sees them | 95% in 30 days | Must |
| BR-02 | Every access records its purpose | Audit findings | Report in a day | Must |
| BR-03 | Consent is checked before export | It is on paper | No export without it | Should |

## Stakeholders

Clinicians, care coordinators and the information governance team, who each
read the referral record for a different reason and own a different decision.

## Assumptions

Medical record numbers are already unique within the network, so the referral
record can be keyed on them without a cleansing programme first.
"""

#: The same kind of document with the parts a reader looks for missing.
THIN_BRD = """Business requirements — referrals

## Background

Referrals get lost.

## Requirements

* The system should be fast and user-friendly.
* Referrals are tracked, etc.
"""

MENU = (DOCS / "08-quality-samples" / "staff-canteen-menu.md").read_text(encoding="utf-8")


class ClassifyTests(SimpleTestCase):
    def test_the_repository_documents_are_typed_as_a_person_would(self):
        for path, expected in LABELLED.items():
            with self.subTest(path):
                text = (DOCS / path).read_text(encoding="utf-8")
                self.assertEqual(classify(text, Path(path).name)["key"], expected)

    def test_a_document_is_typed_by_what_it_says_not_by_its_file_name(self):
        found = classify(BRD, "Scan_0031 (2).docx")
        self.assertEqual(found["key"], "business_requirements")
        self.assertTrue(any("sections" in reason for reason in found["evidence"]))

    def test_its_own_title_outweighs_a_misleading_file_name(self):
        text = (DOCS / "05-release" / "deployment-runbook.md").read_text(encoding="utf-8")
        self.assertEqual(classify(text, "test-plan.md")["key"], "runbook")

    def test_nothing_clearly_ahead_is_other_and_says_what_it_looked_like(self):
        found = classify(MENU, "menu.md")
        self.assertEqual((found["key"], found["confidence"]), ("other", "low"))
        found = classify("## Scope\n\nThe pilot, two clinics, four hundred patients.\n")
        self.assertEqual(found["key"], "other")

    def test_other_has_no_content_checks(self):
        self.assertEqual(checks(MENU, "other"), [])
        self.assertEqual(checks(MENU, "not-a-type"), [])

    def test_every_type_is_judged_by_checks_that_add_up(self):
        for kind in document_types.TYPES:
            with self.subTest(kind.key):
                found = checks(BRD, kind.key)
                self.assertEqual(bool(found), kind.key != "other")
                for check in found:
                    self.assertLessEqual(check.earned, check.points)
                    self.assertTrue(check.rule)
                    if check.state != "good":
                        self.assertTrue(check.improve, check.label)


class BusinessRequirementsTests(SimpleTestCase):
    def check(self, content, key):
        return next(c for c in checks(content, "business_requirements") if c.key == key)

    def test_a_complete_brd_meets_its_checks(self):
        found = {c.key: c for c in checks(BRD, "business_requirements")}
        self.assertEqual(found["type-required"].state, "good")
        self.assertEqual(found["type-identifiers"].finding, "3 identifiers, BR-01 to BR-03.")
        self.assertEqual(found["type-why"].state, "good")
        self.assertEqual(found["type-priority"].state, "good")
        self.assertEqual(found["type-vague"].state, "good")

    def test_missing_sections_are_named(self):
        found = self.check(THIN_BRD, "type-required")
        self.assertEqual((found.earned, found.state), (15, "partial"))
        self.assertIn("no scope", found.finding)
        self.assertIn("thin: purpose or context (line 3", found.finding)
        self.assertIn("“Scope” section", found.improve)

    def test_vague_requirements_are_quoted_with_their_line(self):
        found = self.check(THIN_BRD, "type-vague")
        self.assertEqual(found.state, "missing")
        self.assertIn("“fast”", found.finding)
        self.assertIn("“user-friendly”", found.finding)
        self.assertIn("line 9", found.finding)

    def test_unnumbered_requirements_cannot_be_cited(self):
        self.assertEqual(self.check(THIN_BRD, "type-identifiers").state, "missing")

    def test_constraints_are_not_counted_as_requirements(self):
        text = BRD + "\n## Constraints\n\n* **C-01** No new infrastructure.\n" \
                     "* **C-02** Data stays in the network.\n"
        self.assertEqual(self.check(text, "type-identifiers").finding,
                         "3 identifiers, BR-01 to BR-03.")

    def test_an_id_defined_twice_is_said(self):
        text = BRD.replace("| BR-03 |", "| BR-02 |")
        # A thin BRD as well: the duplicate is said however few there are.
        found = self.check(text, "type-identifiers")
        self.assertEqual(found.state, "partial")
        self.assertIn("BR-02 is defined more than once", found.finding)


class OtherTypeTests(SimpleTestCase):
    def test_a_postmortem_needs_a_timed_timeline_and_owned_actions(self):
        text = ("Postmortem — search outage\n\n## Impact\n\nSearch failed for an hour for "
                "every clinic, and staff telephoned the ward for each record they needed.\n\n"
                "## Timeline\n\nSearch slowed, then failed; it was restarted later that "
                "afternoon by the on-call engineer after the alert fired.\n\n"
                "## Actions\n\n| Action |\n| --- |\n| Bound the limit |\n")
        found = {c.key: c for c in checks(text, "postmortem")}
        self.assertEqual(found["type-timeline"].state, "missing")
        self.assertEqual(found["type-actions"].finding,
                         "The table at line 13 has action but no column for owner and due.")

    def test_non_functional_requirements_name_the_ones_with_no_target(self):
        text = ("Non-functional requirements\n\n| ID | Requirement | Verification |\n"
                "| --- | --- | --- |\n| NFR-01 | p95 under 300ms | Load test |\n"
                "| NFR-02 | Authentication is rate-limited | Review |\n"
                "| NFR-03 | No patient names in logs | Log scan |\n")
        found = next(c for c in checks(text, "non_functional_requirements")
                     if c.key == "type-measurable")
        self.assertEqual(found.state, "missing")
        self.assertIn("NFR-02 does not", found.finding)

    def test_a_runbook_says_when_it_was_last_exercised(self):
        text = (DOCS / "05-release" / "deployment-runbook.md").read_text(encoding="utf-8")
        found = {c.key: c for c in checks(text, "runbook")}
        self.assertEqual(found["type-exercised"].state, "good")
        found = {c.key: c for c in checks(text.replace("Last exercised", "Exercised"),
                                          "runbook")}
        self.assertEqual(found["type-exercised"].state, "missing")


class StoredTests(SimpleTestCase):
    def test_the_stored_check_carries_the_type_and_a_separate_score(self):
        shown = source_quality.assess(BRD, title="upload.docx")
        self.assertEqual(shown["type"]["key"], "business_requirements")
        self.assertEqual(shown["type"]["source"], "rules")
        self.assertEqual(shown["content"]["title"],
                         "Content check · Business requirements document")
        # The source check is unchanged by the type: the same six checks.
        self.assertEqual([c.key for c in shown["checks"]],
                         ["readable", "tables", "structure", "identity", "finished", "limits"])

    def test_a_type_a_person_sets_wins_and_keeps_what_the_rules_said(self):
        shown = source_quality.assess(BRD, document_type="test_plan")
        self.assertEqual((shown["type"]["key"], shown["type"]["source"]), ("test_plan", "person"))
        self.assertEqual(shown["type"]["detected"]["key"], "business_requirements")
        self.assertEqual(shown["content"]["title"], "Content check · Test plan or strategy")

    def test_other_has_no_content_score(self):
        shown = source_quality.assess(MENU)
        self.assertEqual(shown["type"]["key"], "other")
        self.assertIsNone(shown["content"])

    def test_a_stored_check_for_another_type_or_registry_is_stale(self):
        stored = source_quality.run(BRD)
        self.assertTrue(source_quality.current(stored))
        self.assertFalse(source_quality.current(stored, "adr"))
        self.assertFalse(source_quality.current({**stored, "types": 0}))


@override_settings(**SETTINGS)
class DocumentTypeScreenTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)

    def document(self, content=BRD, name="upload.docx"):
        document = Document.objects.create(
            application=self.app, uploaded_by=self.owner, name=name, size=len(content),
            sha256=hashlib.sha256(content.encode()).hexdigest(), status="ready",
            origin="upload",
        )
        return document, add_knowledge(self.owner, self.app.pk, name, content, document=document)

    def test_the_document_page_says_its_type_and_judges_it_as_one(self):
        document, _ = self.document()
        response = self.client.get(reverse("document-detail", args=[self.app.pk, document.pk]))
        self.assertContains(response, 'id="document-type"')
        self.assertContains(response, "Business requirements document")
        self.assertContains(response, 'id="content-check"')
        self.assertContains(response, "Content check · Business requirements document")
        self.assertContains(response, 'name="document_type"')

    def test_the_sources_table_shows_the_type(self):
        self.document()
        response = self.client.get(reverse("documents", args=[self.app.pk]))
        self.assertContains(response, " · Business requirements document</small>")

    def test_an_owner_sets_the_type_and_it_is_checked_again(self):
        document, entry = self.document()
        response = self.client.post(
            reverse("knowledge-detail", args=[self.app.pk, entry.pk]),
            {"action": "set_type", "document_type": "adr", "next": "document"},
        )
        self.assertRedirects(response, reverse("document-detail",
                                               args=[self.app.pk, document.pk]))
        entry.refresh_from_db()
        self.assertEqual(entry.document_type, "adr")
        self.assertEqual(entry.quality["type"]["source"], "person")
        self.assertTrue(any(c["key"] == "type-status" for c in entry.quality["type_checks"]))
        event = AuditEvent.objects.get(action="knowledge.type_set")
        self.assertEqual(event.details, {"document_type": "adr"})
        # And handed back to the rules.
        self.client.post(reverse("knowledge-detail", args=[self.app.pk, entry.pk]),
                         {"action": "set_type", "document_type": ""})
        entry.refresh_from_db()
        self.assertEqual((entry.document_type, entry.quality["type"]["key"]),
                         ("", "business_requirements"))

    def test_an_unknown_type_is_refused_not_stored(self):
        _, entry = self.document()
        response = self.client.post(reverse("knowledge-detail", args=[self.app.pk, entry.pk]),
                                    {"action": "set_type", "document_type": "../secrets"})
        self.assertEqual(response.status_code, 400)
        entry.refresh_from_db()
        self.assertEqual(entry.document_type, "")

    def test_a_viewer_sees_the_type_but_cannot_change_it(self):
        _, entry = self.document()
        self.client.force_login(self.viewer, backend="django.contrib.auth.backends.ModelBackend")
        url = reverse("knowledge-detail", args=[self.app.pk, entry.pk])
        response = self.client.get(url)
        self.assertContains(response, 'id="document-type"')
        self.assertNotContains(response, 'name="document_type"')
        response = self.client.post(url, {"action": "set_type", "document_type": "adr"})
        self.assertIn(response.status_code, {403, 404})
        entry.refresh_from_db()
        self.assertEqual(entry.document_type, "")

    def test_a_typed_record_cannot_be_given_a_document_type(self):
        entry = add_knowledge(self.owner, self.app.pk, "Note", "Refunds expire after 30 days.")
        response = self.client.post(reverse("knowledge-detail", args=[self.app.pk, entry.pk]),
                                    {"action": "set_type", "document_type": "adr"})
        self.assertEqual(response.status_code, 400)

    def test_converting_again_keeps_the_type_a_person_set(self):
        document, entry = self.document()
        KnowledgeEntry.objects.filter(pk=entry.pk).update(document_type="policy")
        with transaction.atomic():
            second = supersede(self.owner, self.app.pk, document, BRD + "\nOne more line.\n")
        self.assertNotEqual(second.pk, entry.pk)
        self.assertEqual(second.document_type, "policy")
        self.assertEqual(second.quality["type"]["key"], "policy")
