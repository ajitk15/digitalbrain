"""Imported tickets say where they came from, and how ready they are to work on."""

import hashlib

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from platform_core.models import Connector, KnowledgeEntry
from platform_core.ticket_quality import assess

from . import test_documents

SETTINGS = dict(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)

READY = """Key: KAN-4
Type: Bug
Status: To Do
Priority: High
Labels: consent regulatory
Updated: 2026-10-03T22:28:21.435+0530

BR-05 requires that the patient's recorded consent decision is honoured before their record
leaves the organisation. GET /fhir/Patient/{id}/$everything checks the fhir:export permission
and returns the bundle without calling consent.current(), so a withheld decision is ignored.
The control matrix rates this as not implemented, and the release notes ask the requesting
party to confirm consent manually, which is not a control because the party asking is the
party who benefits from the answer being yes.
Steps to reproduce: register a patient; record data-exchange granted false; request the export.
Expected 403 and an audit record. Actual 200 with the full bundle.
Acceptance criteria: an export for a withheld patient is refused; the refusal is audited;
a permitted export works unchanged; tests cover granted, withdrawn and never asked."""


class AssessTests(SimpleTestCase):
    def test_a_complete_bug_scores_full_marks(self):
        result = assess("KAN-4 - Patient consent is not checked before a partner export", READY)
        self.assertEqual(result["score"], 100, [c for c in result["checks"] if c.state != "good"])
        self.assertEqual(result["verdict"], "Ready")
        self.assertEqual(result["gaps"], [])

    def test_a_bare_ticket_says_what_is_missing_and_how_to_fix_it(self):
        result = assess("Fix bug", "Key: KAN-1\nType: Bug\n\nIt broke. TBD")
        self.assertLess(result["score"], 20)
        self.assertEqual(result["tone"], "poor")
        # The largest gain comes first, so the advice is in the order worth doing.
        self.assertEqual(result["gaps"][0].key, "acceptance")
        for check in result["gaps"]:
            self.assertTrue(check.finding and check.improve and check.rule, check.key)
        self.assertIn("TBD", next(c for c in result["checks"] if c.key == "placeholders").finding)

    def test_a_story_is_not_marked_down_for_reproduction_steps(self):
        story = READY.replace("Type: Bug", "Type: Story")
        result = assess("Patient consent is checked before a partner export", story)
        self.assertNotIn("reproduction", [check.key for check in result["checks"]])
        self.assertFalse(result["applies_reproduction"])
        self.assertEqual(result["points"], 90)

    def test_a_record_number_is_not_read_as_a_requirement(self):
        text = "Key: KAN-2\nType: Task\n\nThe seed cohort includes MRN RGH-0177310 in ISO-8601."
        rationale = next(c for c in assess("A title of enough words here", text)["checks"]
                         if c.key == "rationale")
        self.assertNotEqual(rationale.state, "good")

    def test_testing_mentioned_only_in_prose_earns_half(self):
        text = READY.replace("; tests cover granted, withdrawn and never asked", "")
        text = text.replace("Steps to reproduce:", "No test fails today. Steps to reproduce:")
        check = next(c for c in assess("A title of enough words here", text)["checks"]
                     if c.key == "tests")
        self.assertEqual((check.state, check.earned), ("partial", 5))


@override_settings(**SETTINGS)
class SourcesTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)

    def connector(self, kind="jira", name="Jira", app=None, **config):
        return Connector.objects.create(
            application=app or self.app,
            kind=kind,
            name=name,
            config=config or {"base_url": "https://team.atlassian.net", "auth": "cloud"},
        )

    def entry(self, source, content=READY, title="KAN-4 - Patient consent is not checked"):
        return KnowledgeEntry.objects.create(
            application=self.app,
            author=self.owner,
            title=title,
            content=content,
            source=source,
            digest=hashlib.sha256(content.encode()).hexdigest(),
        )

    def sources(self):
        """Tickets have their own tab; a record no connector claims stays on Documents."""
        return self.client.get(reverse("tickets", args=[self.app.pk]))

    def documents(self):
        return self.client.get(reverse("documents", args=[self.app.pk]))

    def test_a_jira_ticket_says_it_came_from_jira_and_shows_its_score(self):
        self.connector()
        entry = self.entry("https://team.atlassian.net/browse/KAN-4")
        response = self.sources()
        self.assertContains(response, "Imported from Jira")
        self.assertNotContains(response, "Imported from Jira ·")
        self.assertNotContains(response, "Imported record")
        self.assertContains(response, ">Readiness<svg", html=False)
        self.assertContains(response, '<span class="check-verdict">ready</span>'
                                      '<span class="check-score">100/100</span>', html=False)
        self.assertContains(
            response,
            reverse("knowledge-detail", args=[self.app.pk, entry.pk]) + "#ticket-quality-detail",
        )

    def test_a_named_connector_is_named(self):
        self.connector(name="Platform board")
        self.entry("https://team.atlassian.net/browse/KAN-4")
        self.assertContains(self.sources(), "Imported from Jira · Platform board")

    def test_two_connectors_on_one_site_name_the_system_but_neither_connector(self):
        self.connector(name="Bugs")
        self.connector(name="Stories")
        self.entry("https://team.atlassian.net/browse/KAN-4")
        response = self.sources()
        self.assertContains(response, "Imported from Jira")
        self.assertNotContains(response, "Imported from Jira ·")

    def test_only_jira_tickets_are_scored(self):
        self.connector(kind="github", name="Repo", repository="acme/app")
        self.entry("https://github.com/acme/app/issues/7")
        response = self.sources()
        self.assertContains(response, "Imported from GitHub")
        # No scored ticket on the page, so no column of dashes either.
        self.assertNotContains(response, ">Readiness<svg", html=False)

    def test_a_record_no_connector_claims_stays_an_imported_record(self):
        """A removed connector, or another application's, names nothing."""
        self.connector(app=self.other)
        self.entry("https://team.atlassian.net/browse/KAN-4")
        response = self.documents()
        self.assertContains(response, "Imported record")
        self.assertNotContains(response, "Imported from Jira")
        self.assertNotContains(response, "check-line")

    def test_the_record_page_explains_the_score(self):
        self.connector()
        weak = READY.split("Acceptance criteria:")[0]
        entry = self.entry("https://team.atlassian.net/browse/KAN-4", content=weak)
        response = self.client.get(reverse("knowledge-detail", args=[self.app.pk, entry.pk]))
        # Closed by default, with the score and the biggest gain readable on
        # one line without opening it.
        self.assertContains(response, '<details class="card ticket-quality" id="ticket-quality">')
        self.assertContains(response, 'id="ticket-quality-detail"')
        self.assertContains(response, "start with acceptance criteria (+25)")
        self.assertContains(response, "Imported from Jira")
        self.assertContains(response, "What to fix")
        self.assertContains(response, "Already good")
        self.assertContains(response, "Now: There is no acceptance criteria section.")
        self.assertContains(response, "How the score is worked out")
        # The ticket follows: its fields as chips, its description as text,
        # and the link to Jira reads as the key rather than the whole URL.
        self.assertContains(response, "<li><span>Type</span>Bug</li>", html=False)
        self.assertContains(response, "<li><span>Priority</span>High</li>", html=False)
        self.assertContains(response, '<div class="ticket-description"><p>BR-05 requires')
        self.assertNotContains(response, "Key: KAN-4")
        self.assertContains(
            response,
            '<a href="https://team.atlassian.net/browse/KAN-4" target="_blank" '
            'rel="noreferrer noopener">'
            "KAN-4</a>",
        )

    def test_a_ticket_description_is_escaped(self):
        self.connector()
        entry = self.entry(
            "https://team.atlassian.net/browse/KAN-4",
            content=READY.replace("BR-05 requires", "<script>alert(1)</script> BR-05 requires"),
        )
        response = self.client.get(reverse("knowledge-detail", args=[self.app.pk, entry.pk]))
        self.assertContains(response, "&lt;script&gt;alert(1)&lt;/script&gt;")
        self.assertNotContains(response, "<script>alert(1)")

    def test_a_typed_knowledge_record_has_no_score(self):
        entry = self.entry("", content="Refund requests expire after thirty days.")
        response = self.client.get(reverse("knowledge-detail", args=[self.app.pk, entry.pk]))
        self.assertNotContains(response, "ticket-quality")
