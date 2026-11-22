"""Draft a fix for a Learned question (`learning_draft`, agentic plan, Phase 6).

A scripted model stands in for the provider, so each case runs what a real run
does - the harness, the knowledge tools under the `knowledge_upkeep` rule, the
receipts and steps - and pins what the agent may and may not do: it proposes,
never decides; a proposed source counts only when it was shown, is live and
holds the quote exactly; a draft is checked again when it is drawn; and a
person's own "Should cite" still decides.
"""

import json
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.http import Http404
from django.test import TestCase, override_settings
from django.urls import reverse

from platform_core import gateway, learning, learning_draft
from platform_core.agent_runtime import harness, registry
from platform_core.agent_runtime.scripted import Script
from platform_core.agent_runtime.tools import CitationRecorder, ToolScope, run_tool
from platform_core.models import (
    AgentStep,
    AIConfiguration,
    AIUsage,
    ApplicationGrant,
    AuditEvent,
    BenchmarkQuestion,
    ChatConversation,
    ChatMessage,
    KnowledgeEntry,
    LearningCandidate,
    OrganizationMember,
    User,
)
from platform_core.workbench import add_knowledge

from . import test_documents

QUOTE = "Refund requests expire after thirty days."


def reply(source_id=None, quote="", missing=""):
    return json.dumps({"source_id": source_id, "quote": quote, "missing": missing})


@override_settings(
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    AI_GATEWAY={
        "max_calls_per_application": 4,
        "max_calls_per_provider": 16,
        "slot_wait_seconds": 0,
        "admission_minutes": 15,
    },
)
class DraftTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.contributor = User.objects.create_user("contributor")
        OrganizationMember.objects.create(
            organization=self.app.product.portfolio.organization, user=self.contributor
        )
        ApplicationGrant.objects.create(
            application=self.app, user=self.contributor, role="contributor"
        )
        self.source = add_knowledge(
            self.owner, self.app.pk, "Returns handbook", f"Store policy.\n{QUOTE}\nKeep receipts."
        )
        ApplicationGrant.objects.create(application=self.other, user=self.owner, role="owner")
        self.elsewhere = add_knowledge(
            self.owner, self.other.pk, "Elsewhere", f"Another library. {QUOTE}"
        )
        self.config = AIConfiguration.objects.create(
            application=self.app, purpose=learning_draft.PURPOSE, provider="claude",
            model="claude-sonnet-5", input_rate=Decimal("3"), output_rate=Decimal("15"),
            configured_by=self.owner,
        )
        for patcher in (
            patch("platform_core.ai.provider_credential", return_value="sk-ant-test"),
            patch("platform_core.secrets.application_secret", return_value="sk-ant-test"),
            patch("platform_core.ai.credential_problem", return_value=None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.gap = self.unanswered("How long do customers have to send something back?")

    def unanswered(self, question):
        conversation = ChatConversation.objects.create(
            application=self.app, user=self.viewer, title="t"
        )
        ChatMessage.objects.create(
            application=self.app, user=self.viewer, conversation=conversation, role="user",
            body=question, sequence=1,
        )
        message = ChatMessage.objects.create(
            application=self.app, user=self.viewer, conversation=conversation, role="assistant",
            body="I could not find that.", citations=[], status="complete", sequence=2,
        )
        learning.collect(self.app)
        return LearningCandidate.objects.get(message=message)

    def draft(self, *turns, user=None):
        script = Script(*turns)
        with patch.object(harness, "runtime_for", return_value=script):
            candidate = learning_draft.draft(user or self.owner, self.app.pk, self.gap.pk)
        return script, candidate

    def citing(self, source=None, quote=QUOTE):
        return reply(str((source or self.source).pk), quote)

    def found(self, **kwargs):
        """The turns of a run that searches with other words, then proposes."""
        return [("search_knowledge", {"query": "refund thirty days"})], self.citing(**kwargs)

    # What a draft is ----------------------------------------------------

    def test_a_draft_names_the_source_it_read_and_decides_nothing(self):
        script, candidate = self.draft(
            [("search_knowledge", {"query": "refund thirty days"})], self.citing()
        )
        self.assertEqual(candidate.draft["kind"], "cite")
        self.assertEqual(candidate.draft["source_id"], str(self.source.pk))
        self.assertEqual((candidate.draft["expected"], candidate.draft["quote"]), (
            "Returns handbook", QUOTE
        ))
        # A suggestion only: still waiting, nothing added to the benchmark or sources.
        self.assertEqual(candidate.state, "open")
        self.assertFalse(BenchmarkQuestion.objects.exists())
        self.assertEqual(KnowledgeEntry.objects.filter(application=self.app).count(), 1)
        self.assertEqual(candidate.drafted_by, self.owner)
        # Paid like any agent: a receipt a turn, steps kept with the candidate.
        self.assertEqual(
            AIUsage.objects.filter(application=self.app, purpose="knowledge_upkeep").count(), 2
        )
        self.assertEqual(
            list(AgentStep.objects.filter(candidate=candidate).values_list("tool", "outcome")),
            [("search_knowledge", "found")],
        )
        self.assertEqual(script.given, ["fetch_source", "search_knowledge"])
        self.assertEqual(script.instructions, learning_draft.INSTRUCTIONS)
        event = AuditEvent.objects.get(action="learning.drafted")
        self.assertEqual(event.details, {"kind": "gap", "draft": "cite"})

    def test_it_starts_from_what_keyword_search_finds_and_spends_nothing_on_that(self):
        self.gap = self.unanswered("When do refund requests expire?")
        script, candidate = self.draft(self.citing())
        self.assertIn(str(self.source.pk), script.calls[0]["evidence"])
        self.assertEqual(script.calls[0]["question"], "Question: When do refund requests expire?")
        # What it was handed counts as shown: no search was needed to propose it.
        self.assertEqual(candidate.draft["kind"], "cite")
        self.assertEqual(AIUsage.objects.filter(application=self.app).count(), 1)

    def test_a_source_it_never_read_is_no_proposal_even_when_right(self):
        # Nothing the question says finds the handbook; naming it unread is a guess.
        _, candidate = self.draft(self.citing())
        self.assertEqual(candidate.draft["kind"], "none")

    def test_a_quote_not_in_the_source_is_no_proposal(self):
        _, candidate = self.draft(self.citing(quote="Refunds are allowed for a full year."))
        self.assertEqual(candidate.draft["kind"], "none")

    def test_a_source_it_was_never_shown_is_no_proposal(self):
        # Another application's source, holding the same words, named by id.
        _, candidate = self.draft(self.citing(self.elsewhere))
        self.assertEqual(candidate.draft["kind"], "none")

    def test_with_no_source_it_says_what_is_missing_as_the_ai_said_it(self):
        _, candidate = self.draft(reply(missing="A policy on returns for online orders."))
        self.assertEqual(
            candidate.draft, {**candidate.draft, "kind": "missing",
                              "note": "A policy on returns for online orders."}
        )

    def test_a_note_with_personal_data_is_not_kept(self):
        _, candidate = self.draft(reply(missing="Ask about MRN: 4471234 directly."))
        self.assertEqual(candidate.draft["kind"], "none")
        self.assertNotIn("4471234", json.dumps(candidate.draft))

    def test_a_reply_that_is_not_json_is_said_and_costs_its_turn(self):
        _, candidate = self.draft("The handbook covers it.")
        self.assertEqual(candidate.draft["kind"], "unreadable")
        self.assertEqual(AIUsage.objects.filter(application=self.app).count(), 1)

    def test_out_of_turns_it_still_answers_from_what_it_read(self):
        searching = [[("search_knowledge", {"query": "refund"})]] * (learning_draft.TOOL_TURNS + 1)
        script = Script(*searching, then=[self.citing()])
        with patch.object(harness, "runtime_for", return_value=script):
            candidate = learning_draft.draft(self.owner, self.app.pk, self.gap.pk)
        self.assertEqual(candidate.draft["kind"], "cite")
        self.assertEqual([call["given"] for call in script.calls][1:], [[]])

    # Who may, and on what --------------------------------------------------

    def test_a_contributor_may_draft_and_a_viewer_may_not(self):
        _, candidate = self.draft(*self.found(), user=self.contributor)
        self.assertEqual(candidate.draft["kind"], "cite")
        with self.assertRaises(PermissionDenied):
            self.draft(*self.found(), user=self.viewer)
        self.assertEqual(AIUsage.objects.filter(actor=self.viewer).count(), 0)

    def test_another_application_reads_as_not_found(self):
        stranger = User.objects.create_user("stranger")
        with self.assertRaises(Http404):
            self.draft(*self.found(), user=stranger)

    def test_only_a_waiting_question_is_drafted(self):
        learning.reject(self.owner, self.app.pk, self.gap.pk)
        with self.assertRaises(ValidationError):
            self.draft(self.citing())
        self.assertFalse(AIUsage.objects.exists())

    def test_with_the_task_switched_off_nothing_is_sent(self):
        AIConfiguration.objects.filter(pk=self.config.pk).update(enabled=False)
        self.assertIn("switched off", learning_draft.drafting_problem(self.owner, self.app))
        with self.assertRaises(ValidationError):
            self.draft(self.citing())
        self.assertFalse(AIUsage.objects.exists())

    def test_a_credential_gone_since_the_page_was_drawn_is_said_not_a_fault(self):
        from platform_core.ai import MissingCredential

        with patch(
            "platform_core.ai.provider_credential",
            side_effect=MissingCredential("Add a Claude credential on Credentials."),
        ):
            response = self.client.post(
                reverse("learned", args=[self.app.pk]),
                {"candidate": self.gap.pk, "action": "draft"},
            )
        self.assertContains(response, "Add a Claude credential", status_code=200)
        self.assertFalse(AIUsage.objects.exists())

    def test_with_no_configuration_the_page_says_where_to_set_it(self):
        self.config.delete()
        self.assertIn("AI settings", learning_draft.drafting_problem(self.owner, self.app))

    def test_its_tools_are_held_to_the_upkeep_rule_on_every_call(self):
        tools = registry.TOOLS
        scope = ToolScope(self.viewer.pk, self.app.pk, learning_draft.PURPOSE)
        self.assertEqual(
            sorted(spec.name for spec in registry.for_scope(scope)),
            ["fetch_source", "search_knowledge"],
        )
        # A viewer cannot draft, so a scope naming one is refused at the tool too.
        with self.assertRaises(PermissionDenied):
            run_tool(
                tools["search_knowledge"], scope, {"query": "refund"}, CitationRecorder(scope)
            )
        self.assertIn(learning_draft.PURPOSE, gateway.PURPOSES)

    # Shown, and decided -----------------------------------------------------

    def test_a_draft_is_checked_again_when_it_is_shown(self):
        _, candidate = self.draft(*self.found())
        self.assertEqual(learning_draft.shown(candidate, self.owner)["kind"], "cite")
        KnowledgeEntry.objects.filter(pk=self.source.pk).update(sensitivity="restricted")
        self.assertEqual(learning_draft.shown(candidate, self.owner)["kind"], "stale")
        KnowledgeEntry.objects.filter(pk=self.source.pk).update(
            sensitivity="", content="Store policy.\nRefunds within a year.", digest="changed"
        )
        self.assertEqual(learning_draft.shown(candidate, self.owner)["kind"], "stale")

    def test_the_page_drafts_with_a_plain_form_and_fills_in_should_cite(self):
        url = reverse("learned", args=[self.app.pk])
        page = self.client.get(url)
        self.assertContains(page, 'value="draft"')
        script = Script(*self.found())
        with patch.object(harness, "runtime_for", return_value=script):
            response = self.client.post(url, {"candidate": self.gap.pk, "action": "draft"})
        self.assertRedirects(response, f"{url}#learned-{self.gap.pk}")
        page = self.client.get(url)
        self.assertContains(page, "Drafted by AI")
        self.assertContains(page, QUOTE)
        self.assertContains(page, 'value="Returns handbook"')
        self.assertContains(page, "Draft again")

    def test_without_a_working_task_the_page_offers_no_button_and_says_why(self):
        AIConfiguration.objects.filter(pk=self.config.pk).update(enabled=False)
        page = self.client.get(reverse("learned", args=[self.app.pk]))
        self.assertNotContains(page, 'value="draft"')
        self.assertContains(page, "Knowledge upkeep is switched off")

    def test_accepting_says_whether_the_drafted_source_was_cited(self):
        self.draft(*self.found())
        accepted = learning.accept(
            self.owner, self.app.pk, self.gap.pk, expected="returns handbook"
        )
        self.assertTrue(accepted.draft["accepted_as_drafted"])
        other = self.unanswered("Who approves a refund over the limit?")
        self.gap = other
        self.draft(*self.found())
        changed = learning.accept(self.owner, self.app.pk, other.pk, expected="Finance approvals")
        self.assertFalse(changed.draft["accepted_as_drafted"])
        self.assertEqual(BenchmarkQuestion.objects.count(), 2)

    def test_the_report_compares_drafted_questions_with_undrafted_ones(self):
        self.draft(*self.found())
        learning.accept(self.owner, self.app.pk, self.gap.pk, expected="Returns handbook")
        plain = self.unanswered("Is there a restocking fee?")
        learning.reject(self.owner, self.app.pk, plain.pk)
        self.assertEqual(
            learning_draft.report(self.app),
            {
                "drafted": {"decided": 1, "accepted": 1, "as_drafted": 1},
                "rules": {"decided": 1, "accepted": 0, "as_drafted": 0},
            },
        )
        out = StringIO()
        call_command("learning_upkeep", str(self.app.pk), stdout=out)
        self.assertIn(
            "Drafted: 1 decided, 1 accepted (100%), 1 with the drafted source", out.getvalue()
        )
        self.assertIn("No draft: 1 decided, 0 accepted (0%)", out.getvalue())
        self.assertIn("too few to judge", out.getvalue())
