"""Code Factory evidence says where it came from.

Every citation a gap item can carry is labelled: the knowledge graph (with the
system its passage was imported from - a document, Jira, GitHub, ServiceNow),
the code graph (a file in the pinned snapshot), or the ticket itself. The model
is shown the label; the reviewer sees it on every item; approval re-checks code
citations against the snapshot just as it re-checks knowledge.
"""

import hashlib
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from platform_core.code_factory import code_citations, source_system, start_run
from platform_core.factory_views import code_source_holds
from platform_core.models import (
    CodeFile,
    CodeRepository,
    CodeSnapshot,
    KnowledgeEntry,
    PlanItem,
)
from platform_core.templatetags.workspace import evidence_origins

from .test_code_factory import QUOTE, SETTINGS, PipelineTests

CODE = (
    "import queue\n\n\nclass Publisher:\n    def send(self, message):\n"
    "        # Queue Beta has no bound: retries pile up here.\n"
    "        self.queue.put(message)\n"
)


class SourceSystemTests(SimpleTestCase):
    def entry(self, source="", document_id=None):
        return KnowledgeEntry(source=source, document_id=document_id)

    def test_each_system_is_read_from_the_record_itself(self):
        self.assertEqual(source_system(self.entry("https://x.atlassian.net/browse/OPS-1")), "Jira")
        self.assertEqual(source_system(self.entry("https://github.com/a/b/issues/3")), "GitHub")
        self.assertEqual(
            source_system(self.entry("https://x.service-now.com/nav_to.do?uri=incident.do")),
            "ServiceNow",
        )
        self.assertEqual(source_system(self.entry(document_id=1)), "Document")
        self.assertEqual(source_system(self.entry()), "Knowledge")

    def test_a_citation_from_before_labelling_counts_as_knowledge_graph(self):
        counts = evidence_origins(
            [
                {"graph_version": "3", "title": "old"},
                {"origin": "ticket", "system": "Jira"},
                {"origin": "code_graph"},
                {"origin": "code_graph"},
            ]
        )
        self.assertEqual(
            [(row["label"], row["count"]) for row in counts],
            [("Jira ticket", 1), ("Knowledge graph", 1), ("Code graph", 2)],
        )


@override_settings(**SETTINGS)
class LabelledEvidenceTests(TestCase):
    setUp = PipelineTests.setUp
    answers = PipelineTests.answers
    run_pipeline = PipelineTests.run_pipeline

    def item(self, cited):
        return [
            {
                "category": "stated",
                "rubric": "",
                "title": "Bound the retry loop",
                "explanation": "The ticket asks for the overflow to stop.",
                "severity": "high",
                "evidence": [{"source_id": number} for number in cited],
            }
        ]

    def test_graph_evidence_is_labelled_with_its_version_and_system(self):
        run = self.run_pipeline()
        citation = PlanItem.objects.filter(plan=run.plan).first().citations[0]
        self.assertEqual(citation["origin"], "knowledge_graph")
        self.assertEqual(citation["origin_label"], "Knowledge graph v1")
        self.assertEqual(citation["system"], "Knowledge")
        self.assertIn(QUOTE, citation["excerpt"])

    def test_an_item_can_cite_the_jira_ticket_and_it_says_so(self):
        run = self.run_pipeline(answers=self.answers(self.item(["1", "2"])))
        graph, ticket = PlanItem.objects.get(plan=run.plan).citations
        self.assertEqual(graph["origin"], "knowledge_graph")
        self.assertEqual(ticket["origin"], "ticket")
        self.assertEqual(ticket["origin_label"], "Jira OPS-12")
        self.assertIn(ticket["excerpt"], self.ticket.content)
        self.assertEqual(ticket["digest"], self.ticket.digest)

    def test_the_model_is_shown_where_each_piece_came_from(self):
        seen = []
        replies = iter(self.answers())

        def capture(user, app_id, purpose, question, citations, **options):
            seen.append(citations)
            return next(replies)

        run = start_run(self.owner, self.app.pk, self.ticket)
        with patch("platform_core.ai.invoke_ai", side_effect=capture):
            from platform_core.code_factory import execute

            execute(run)
        analysis = seen[1]
        self.assertTrue(analysis[0]["title"].startswith("[Knowledge graph v1]"))
        self.assertTrue(analysis[1]["title"].startswith("[Jira OPS-12]"))

    def test_a_hand_written_ticket_is_never_evidence(self):
        from platform_core.code_factory import ticket_citation

        run = start_run(self.owner, self.app.pk, self.ticket)
        run.ticket_body = "Typed in on the screen."
        self.assertEqual(ticket_citation(run), [])

    def test_the_evidence_card_lists_each_source_once_with_what_it_gave(self):
        """Not a total beside a breakdown of the same total: one row per source."""
        run = self.run_pipeline()  # two gaps, each quoting graph passage 1
        page = self.client.get(reverse("run-detail", args=[self.app.pk, run.pk]))
        self.assertContains(page, "Each source, and how many verified quotations")
        self.assertContains(page, "2 quotes")
        self.assertContains(page, "read, not quoted")  # the Jira ticket
        self.assertContains(page, "none pinned")  # no code snapshot
        self.assertNotContains(page, "Verified quotes")

    def test_the_item_page_and_the_run_page_show_the_labels(self):
        run = self.run_pipeline(answers=self.answers(self.item(["1", "2"])))
        item = PlanItem.objects.get(plan=run.plan)
        page = self.client.get(reverse("plan-item", args=[self.app.pk, run.plan.pk, item.pk]))
        self.assertContains(page, "origin-knowledge_graph")
        self.assertContains(page, "Knowledge graph v1")
        self.assertContains(page, "Jira OPS-12")
        detail = self.client.get(reverse("run-detail", args=[self.app.pk, run.pk]))
        self.assertContains(detail, "Jira ticket · 1")
        self.assertContains(detail, "Knowledge graph · 1")


@override_settings(**SETTINGS)
class WhereTheTicketSaysItTests(TestCase):
    """A gap stated in the ticket points back at the sentences that state it."""

    setUp = PipelineTests.setUp
    answers = PipelineTests.answers
    run_pipeline = PipelineTests.run_pipeline

    def stated(self, title, explanation, category="stated"):
        return self.answers(
            [
                {
                    "category": category,
                    "rubric": "",
                    "title": title,
                    "explanation": explanation,
                    "severity": "high",
                    "evidence": [{"source_id": "1"}],
                }
            ]
        )

    def page(self, run):
        item = PlanItem.objects.get(plan=run.plan)
        return self.client.get(reverse("plan-item", args=[self.app.pk, run.plan.pk, item.pk]))

    def test_the_sentence_is_shown_with_its_shared_words_marked(self):
        run = self.run_pipeline(
            answers=self.stated("Stop Queue Beta overflowing", "Retries overflow Queue Beta.")
        )
        page = self.page(run)
        self.assertContains(page, "Where the ticket says this")
        self.assertContains(page, "Jira OPS-12")
        self.assertContains(page, '<blockquote class="ticket-quote">')
        self.assertContains(page, "<mark>Queue</mark>")
        # "overflows" in the ticket matches "overflow" in the gap.
        self.assertContains(page, "<mark>overflows</mark>")

    def test_a_gap_found_by_comparison_is_not_pointed_into_the_ticket(self):
        run = self.run_pipeline(
            answers=self.stated("Queue Beta has no bound", "Overflows.", category="functional")
        )
        self.assertNotContains(self.page(run), "Where the ticket says this")

    def test_nothing_alike_says_so_rather_than_pointing_anywhere(self):
        run = self.run_pipeline(answers=self.stated("Rotate the signing keys", "Keys expire."))
        page = self.page(run)
        self.assertContains(page, "No sentence in the ticket shares enough words")
        self.assertNotContains(page, '<blockquote class="ticket-quote">')

    def test_the_run_page_tags_a_stated_gap_with_its_ticket(self):
        run = self.run_pipeline(
            answers=self.stated("Stop Queue Beta overflowing", "Retries overflow Queue Beta.")
        )
        page = self.client.get(reverse("run-detail", args=[self.app.pk, run.pk]))
        tag = 'title="The analysis read this gap as stated in the ticket">Jira OPS-12'
        self.assertContains(page, tag)


@override_settings(**SETTINGS)
class CodeGraphEvidenceTests(TestCase):
    setUp = PipelineTests.setUp

    def pinned(self, app=None):
        repository = CodeRepository.objects.create(
            application=app or self.app,
            provider="github",
            external_id="acme/widgets",
            name="acme/widgets",
            source_url="https://github.com/acme/widgets",
            added_by=self.owner,
            status="ready",
        )
        snapshot = CodeSnapshot.objects.create(number=4, repository=repository, commit_sha="c" * 40)
        code = CodeFile.objects.create(
            snapshot=snapshot,
            path="widgets/publisher.py",
            language="python",
            digest=hashlib.sha256(CODE.encode()).hexdigest(),
            content=CODE,
            lines=CODE.count("\n"),
        )
        return snapshot, code

    def test_a_pinned_file_is_offered_as_labelled_code_evidence(self):
        snapshot, code = self.pinned()
        run = start_run(self.owner, self.app.pk, self.ticket)
        run.code_snapshot = snapshot
        (citation,) = code_citations(run, "Queue Beta overflows under retry load")
        self.assertEqual(citation["origin"], "code_graph")
        self.assertEqual(citation["origin_label"], "Code graph · acme/widgets v4")
        self.assertEqual(citation["id"], f"code:{code.pk}")
        self.assertIn("Queue Beta has no bound", citation["excerpt"])
        self.assertIn(citation["excerpt"], code.content)

    def test_approval_re_checks_a_code_citation_against_the_snapshot(self):
        _, code = self.pinned()
        source = {"id": f"code:{code.pk}", "digest": code.digest}
        self.assertTrue(code_source_holds(self.app, source))
        self.assertFalse(code_source_holds(self.app, {**source, "digest": "0" * 64}))
        # A file in another application's snapshot never holds here.
        self.assertFalse(code_source_holds(self.other, source))
        code.delete()
        self.assertFalse(code_source_holds(self.app, source))

    def test_no_snapshot_means_no_code_evidence(self):
        run = start_run(self.owner, self.app.pk, self.ticket)
        self.assertEqual(code_citations(run, "Queue Beta"), [])


@override_settings(**SETTINGS)
class CodeGraphFileTagTests(TestCase):
    """Files a plan touches, and files implementation rewrote, say when they came
    from the code graph rather than the live repository or nowhere at all."""

    setUp = PipelineTests.setUp
    answers = PipelineTests.answers
    run_pipeline = PipelineTests.run_pipeline
    pinned = CodeGraphEvidenceTests.pinned

    def run_on_snapshot(self):
        run = self.run_pipeline()  # the design names queue.py for every item
        snapshot, _ = self.pinned()
        CodeFile.objects.create(
            snapshot=snapshot,
            path="queue.py",
            language="python",
            digest="d" * 64,
            content="LIMIT = None\n",
            lines=1,
        )
        run.code_snapshot = snapshot
        run.save(update_fields=["code_snapshot"])
        return run

    def test_a_target_in_the_snapshot_is_tagged_code_graph(self):
        run = self.run_on_snapshot()
        item = PlanItem.objects.filter(plan=run.plan).first()
        page = self.client.get(reverse("plan-item", args=[self.app.pk, run.plan.pk, item.pk]))
        self.assertContains(page, ">Code graph</span>")
        self.assertNotContains(page, ">New file</span>")
        detail = self.client.get(reverse("plan-detail", args=[self.app.pk, run.plan.pk]))
        self.assertContains(detail, ">Code graph</span>")

    def test_a_target_the_snapshot_lacks_is_a_new_file(self):
        run = self.run_on_snapshot()
        PlanItem.objects.filter(plan=run.plan).update(targets=["queue_limits.py"])
        item = PlanItem.objects.filter(plan=run.plan).first()
        page = self.client.get(reverse("plan-item", args=[self.app.pk, run.plan.pk, item.pk]))
        self.assertContains(page, ">New file</span>")

    def test_a_change_read_from_the_snapshot_says_so_not_that_the_file_is_new(self):
        from platform_core.models import FactoryRun, ProposedChange

        run = self.run_on_snapshot()
        FactoryRun.objects.filter(pk=run.pk).update(status="prepared")
        ProposedChange.objects.create(run=run, path="queue.py", content="LIMIT = 100\n")
        ProposedChange.objects.create(run=run, path="limits.py", content="MAX = 1\n")
        page = self.client.get(reverse("run-detail", args=[self.app.pk, run.pk]))
        self.assertContains(page, "Code graph · acme/widgets v4")
        self.assertContains(page, "rewrites the file as acme/widgets snapshot v4 holds it")
        # The path the snapshot does not hold is still a new file.
        self.assertContains(page, "this file does not exist yet")
