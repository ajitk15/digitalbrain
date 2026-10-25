"""What use teaches comes back as candidates a person decides on (`learning.py`).

These pin the rules that keep the loop from contaminating what it learns from:
candidates are written by rules and only from records that qualify, nothing is
knowledge until an owner or contributor accepts it, a rejection is remembered, a
learned source is never requirement evidence, a withdrawn signal withdraws what
was learned from it, and a question with personal data is never copied.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from platform_core import learning
from platform_core.code_knowledge import LEARNED_PREFIX, derived
from platform_core.models import (
    ApplicationGrant,
    AuditEvent,
    BenchmarkQuestion,
    ChangePlan,
    ChatConversation,
    ChatMessage,
    FactoryRun,
    KnowledgeEntry,
    LearningCandidate,
    OrganizationMember,
    PlanItem,
    ProposedChange,
    TriageHypothesis,
    TriageRun,
    TriageVerdict,
    User,
)
from platform_core.source_kinds import visible
from platform_core.workbench import add_knowledge

from .test_documents import DocumentTests


@override_settings(
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
)
class LearningTests(TestCase):
    def setUp(self):
        DocumentTests.setUp(self)
        self.contributor = User.objects.create_user("contributor")
        OrganizationMember.objects.create(
            organization=self.app.product.portfolio.organization, user=self.contributor
        )
        ApplicationGrant.objects.create(
            application=self.app, user=self.contributor, role="contributor"
        )

    # Signals -------------------------------------------------------------

    def delivered(self, checks=None, status="delivered", plan_status="approved", **fields):
        plan = ChangePlan.objects.create(
            application=self.app,
            author=self.owner,
            title="Check consent before export",
            proposal="p",
            validation="v",
            digest="d",
            status=plan_status,
            reviewed_by=self.contributor,
            reviewed_at=timezone.now(),
        )
        PlanItem.objects.create(
            plan=plan, sequence=1, category="stated", title="Refuse an export without consent",
            explanation="e", change_summary="c", status="accepted",
        )
        PlanItem.objects.create(
            plan=plan, sequence=2, category="functional", title="A rejected idea",
            explanation="e", change_summary="c", status="rejected",
        )
        run = FactoryRun.objects.create(
            application=self.app,
            requested_by=self.owner,
            number=FactoryRun.objects.count() + 1,
            ticket_external_id="CARE-401",
            ticket_title="Partner export ignores consent",
            plan=plan,
            status=status,
            pull_request_url=fields.pop("pull_request_url", "https://github.com/o/r/pull/7"),
            checks=[{"name": "tests", "conclusion": "success"}] if checks is None else checks,
            graph_version=3,
            **fields,
        )
        ProposedChange.objects.create(run=run, path="carepath/export.py", content="x")
        return run

    def confirmed(self, verdict="accepted"):
        incident = add_knowledge(
            self.owner, self.app.pk, "INC0042 Export failures", "Exports failed at 09:00."
        )
        cause = add_knowledge(
            self.owner, self.app.pk, "CHG0007 Consent service rollout", "Consent v2 rolled out."
        )
        run = TriageRun.objects.create(
            application=self.app, incident=incident, requested_by=self.owner,
            incident_digest=incident.digest, pack_digest="p",
        )
        hypothesis = TriageHypothesis.objects.create(
            run=run, rank=1, statement="The consent rollout broke exports.", next_step="Roll back",
            citations=[{"id": str(cause.pk), "quote": "Consent v2 rolled out."}],
        )
        return TriageVerdict.objects.create(
            hypothesis=hypothesis, user=self.owner, verdict=verdict, note="Rolled back at 09:40.",
            actual_cause=cause, confirmed_at=timezone.now(),
        )

    def answered(self, question="How long do refunds take?", citations=(), status="complete"):
        conversation = ChatConversation.objects.create(
            application=self.app, user=self.viewer, title="t"
        )
        ChatMessage.objects.create(
            application=self.app, user=self.viewer, conversation=conversation, role="user",
            body=question, sequence=1,
        )
        return ChatMessage.objects.create(
            application=self.app, user=self.viewer, conversation=conversation, role="assistant",
            body="I could not find that.", citations=list(citations), status=status, sequence=2,
        )

    # Collecting ----------------------------------------------------------

    def test_a_delivered_change_becomes_a_change_record_written_by_rules(self):
        run = self.delivered()
        self.assertEqual(learning.collect(self.app), 1)
        candidate = LearningCandidate.objects.get(run=run)
        self.assertEqual((candidate.kind, candidate.state), ("change", "open"))
        self.assertEqual(candidate.title, "Change record: CARE-401 Partner export ignores consent")
        for line in (
            "Type: Change record",
            "Ticket: CARE-401",
            "Pull request: https://github.com/o/r/pull/7",
            "Plan approved by: contributor",
            "- carepath/export.py",
            "- tests: success",
            # What the plan said is quoted as what it said, never stated.
            '- "Refuse an export without consent"',
        ):
            self.assertIn(line, candidate.body)
        # A rejected plan item is a judgement, not a fact: never in the record.
        self.assertNotIn("A rejected idea", candidate.body)

    def test_only_a_finished_passing_delivery_after_an_approved_plan_qualifies(self):
        self.delivered(checks=[{"name": "tests", "conclusion": None}])
        self.delivered(checks=[{"name": "tests", "conclusion": "failure"}])
        self.delivered(plan_status="rejected")
        self.delivered(status="prepared")
        self.delivered(pull_request_url="")
        self.assertEqual(learning.collect(self.app), 0)

    def test_a_folder_delivery_never_names_the_servers_path(self):
        run = self.delivered(pull_request_url="", delivered_to="C:/srv/carepath", checks=[])
        learning.collect(self.app)
        body = LearningCandidate.objects.get(run=run).body
        self.assertIn("Delivered into the repository's folder on the server", body)
        self.assertNotIn("C:/srv", body)
        self.assertIn("- none reported", body)

    def test_a_confirmed_cause_becomes_an_incident_learning(self):
        verdict = self.confirmed()
        learning.collect(self.app)
        candidate = LearningCandidate.objects.get(verdict=verdict)
        self.assertEqual(candidate.kind, "incident")
        for line in (
            "Incident: INC0042 Export failures",
            "Confirmed cause: CHG0007 Consent service rollout",
            '"The consent rollout broke exports."',
            "Rolled back at 09:40.",
            "- CHG0007 Consent service rollout",
        ):
            self.assertIn(line, candidate.body)

    def test_a_rejected_verdict_teaches_nothing(self):
        self.confirmed(verdict="rejected")
        self.assertEqual(learning.collect(self.app), 0)

    def test_an_unanswered_question_is_a_gap_without_who_asked(self):
        message = self.answered()
        self.answered(citations=[{"id": "x", "title": "Refund policy"}])
        self.answered(status="failed")
        self.assertEqual(learning.collect(self.app), 1)
        gap = LearningCandidate.objects.get(message=message)
        self.assertEqual((gap.kind, gap.question), ("gap", "How long do refunds take?"))
        self.assertIsNone(gap.reported_by)

    def test_a_question_with_personal_data_is_never_copied(self):
        message = self.answered("What happened to MRN: 4471234 last week?")
        learning.collect(self.app)
        candidate = LearningCandidate.objects.get(message=message)
        self.assertEqual((candidate.state, candidate.question), ("withdrawn", ""))
        self.assertNotIn("4471234", candidate.title)

    def test_the_same_question_asked_twice_waits_once(self):
        self.answered()
        self.answered("how long do refunds take?")
        learning.collect(self.app)
        self.assertEqual(LearningCandidate.objects.filter(state="open").count(), 1)

    def test_collecting_again_offers_nothing_twice_and_makes_no_knowledge(self):
        self.delivered()
        self.confirmed()
        self.answered()
        before = KnowledgeEntry.objects.count()
        self.assertEqual(learning.collect(self.app), 3)
        self.assertEqual(learning.collect(self.app), 0)
        self.assertEqual(KnowledgeEntry.objects.count(), before)

    # Deciding ------------------------------------------------------------

    def test_accepting_a_change_record_adds_a_derived_source_and_audits(self):
        run = self.delivered()
        learning.collect(self.app)
        candidate = LearningCandidate.objects.get(run=run)
        learning.accept(self.contributor, self.app.pk, candidate.pk, note="Useful")
        candidate.refresh_from_db()
        self.assertEqual(candidate.state, "accepted")
        entry = candidate.entry
        self.assertEqual(entry.source, f"{LEARNED_PREFIX}change/{run.pk}")
        self.assertEqual(entry.content, candidate.body)
        self.assertTrue(KnowledgeEntry.objects.filter(derived(), pk=entry.pk).exists())
        self.assertTrue(
            AuditEvent.objects.filter(action="learning.accepted", actor=self.contributor).exists()
        )
        self.assertEqual(learning.learned_entries(self.app), 1)

    def test_a_learned_source_is_never_requirement_evidence(self):
        from platform_core.code_knowledge import references
        from platform_core.models import GraphRevision

        run = self.delivered()
        learning.collect(self.app)
        entry = learning.accept(
            self.owner, self.app.pk, LearningCandidate.objects.get(run=run).pk
        ).entry
        line = entry.content.splitlines()[0]
        revision = GraphRevision.objects.create(
            application=self.app, number=1, fingerprint="f",
            data={"edges": [{"knowledge_id": str(entry.pk), "evidence": line, "line": 1,
                             "digest": entry.digest, "source": "a", "target": "b"}]},
        )
        self.assertEqual(list(references(revision)), [])

    def test_a_question_joins_the_benchmark_only_with_the_source_it_should_cite(self):
        message = self.answered()
        learning.collect(self.app)
        gap = LearningCandidate.objects.get(message=message)
        with self.assertRaises(ValidationError):
            learning.accept(self.owner, self.app.pk, gap.pk)
        learning.accept(self.owner, self.app.pk, gap.pk, expected="Refund policy")
        question = BenchmarkQuestion.objects.get(application=self.app)
        self.assertEqual(
            (question.question, question.expected), ("How long do refunds take?", "Refund policy")
        )
        self.assertTrue(AuditEvent.objects.filter(action="benchmark.question_added").exists())

    def test_a_rejection_is_remembered_and_never_offered_again(self):
        run = self.delivered()
        learning.collect(self.app)
        candidate = LearningCandidate.objects.get(run=run)
        learning.reject(self.owner, self.app.pk, candidate.pk, "Not worth keeping")
        self.assertEqual(learning.collect(self.app), 0)
        with self.assertRaises(ValidationError):
            learning.accept(self.owner, self.app.pk, candidate.pk)
        self.assertTrue(AuditEvent.objects.filter(action="learning.rejected").exists())

    def test_a_viewer_cannot_decide_and_another_application_reads_as_not_found(self):
        from django.http import Http404

        run = self.delivered()
        learning.collect(self.app)
        candidate = LearningCandidate.objects.get(run=run)
        with self.assertRaises(PermissionDenied):
            learning.accept(self.viewer, self.app.pk, candidate.pk)
        ApplicationGrant.objects.create(application=self.other, user=self.owner, role="owner")
        with self.assertRaises(LearningCandidate.DoesNotExist):
            learning.accept(self.owner, self.other.pk, candidate.pk)
        stranger = User.objects.create_user("stranger")
        with self.assertRaises(Http404):
            learning.accept(stranger, self.app.pk, candidate.pk)

    # Withdrawing ---------------------------------------------------------

    def test_a_retracted_verdict_retires_what_was_learned_from_it(self):
        verdict = self.confirmed()
        learning.collect(self.app)
        candidate = LearningCandidate.objects.get(verdict=verdict)
        entry = learning.accept(self.owner, self.app.pk, candidate.pk).entry
        verdict.delete()
        learning.withdraw(self.app)
        candidate.refresh_from_db()
        entry.refresh_from_db()
        self.assertEqual(candidate.state, "withdrawn")
        self.assertFalse(entry.active)

    def test_a_removed_conversation_takes_its_question_out_of_the_inbox(self):
        message = self.answered()
        learning.collect(self.app)
        candidate = LearningCandidate.objects.get(message=message)
        message.conversation.messages.all().delete()
        learning.withdraw(self.app)
        candidate.refresh_from_db()
        self.assertEqual((candidate.state, candidate.question), ("withdrawn", ""))

    # Reporting an answer -------------------------------------------------

    def test_the_asker_can_mark_an_answer_wrong_and_it_waits_for_a_decision(self):
        message = self.answered(citations=[{"id": "x", "title": "Old runbook"}])
        self.client.force_login(self.viewer, backend="django.contrib.auth.backends.ModelBackend")
        url = reverse("chat-report", args=[self.app.pk, message.pk])
        self.assertContains(self.client.get(url), "Report this answer")
        response = self.client.post(url, {"note": "Cites the old one", "expected": "runbook-v2"})
        self.assertEqual(response.status_code, 302)
        candidate = LearningCandidate.objects.get(message=message)
        self.assertEqual(
            (candidate.kind, candidate.note, candidate.expected, candidate.body),
            ("wrong", "Cites the old one", "runbook-v2", "Cited: Old runbook"),
        )
        self.assertEqual(candidate.reported_by, self.viewer)
        self.assertTrue(AuditEvent.objects.filter(action="chat.answer_reported").exists())
        self.assertFalse(BenchmarkQuestion.objects.exists())

    def test_nobody_reports_someone_elses_answer(self):
        message = self.answered()
        url = reverse("chat-report", args=[self.app.pk, message.pk])
        self.assertEqual(self.client.post(url, {"note": "x"}).status_code, 404)

    def test_a_report_with_personal_data_is_refused(self):
        message = self.answered()
        self.client.force_login(self.viewer, backend="django.contrib.auth.backends.ModelBackend")
        response = self.client.post(
            reverse("chat-report", args=[self.app.pk, message.pk]),
            {"note": "Patient name: Jane Example was in it"},
        )
        self.assertContains(response, "personal data")
        self.assertFalse(LearningCandidate.objects.filter(kind="wrong").exists())

    def test_a_complete_answer_offers_the_report_link(self):
        message = self.answered()
        self.client.force_login(self.viewer, backend="django.contrib.auth.backends.ModelBackend")
        page = self.client.get(
            f"{reverse('chat', args=[self.app.pk])}?conversation={message.conversation_id}"
        )
        self.assertContains(page, reverse("chat-report", args=[self.app.pk, message.pk]))

    # The inbox -----------------------------------------------------------

    def test_the_inbox_collects_and_decides_with_plain_forms(self):
        run = self.delivered()
        url = reverse("learned", args=[self.app.pk])
        page = self.client.get(url)
        self.assertContains(page, "Change record: CARE-401")
        candidate = LearningCandidate.objects.get(run=run)
        response = self.client.post(url, {"candidate": candidate.pk, "action": "accept"})
        self.assertRedirects(response, url)
        candidate.refresh_from_db()
        self.assertEqual(candidate.state, "accepted")
        self.assertContains(self.client.get(url), "The source")

    def test_a_refused_decision_is_shown_on_the_page(self):
        self.answered()
        url = reverse("learned", args=[self.app.pk])
        self.client.get(url)
        gap = LearningCandidate.objects.get(kind="gap")
        response = self.client.post(url, {"candidate": gap.pk, "action": "accept"})
        self.assertContains(response, "Name the source it should cite", status_code=200)

    def test_the_inbox_and_its_tab_are_for_owners_and_contributors(self):
        url = reverse("learned", args=[self.app.pk])
        tabs = self.client.get(reverse("documents", args=[self.app.pk])).content.decode()
        self.assertIn(url, tabs)
        self.client.force_login(self.viewer, backend="django.contrib.auth.backends.ModelBackend")
        self.assertEqual(self.client.get(url).status_code, 403)
        tabs = self.client.get(reverse("documents", args=[self.app.pk])).content.decode()
        self.assertNotIn(url, tabs)
        viewer = ApplicationGrant.objects.get(application=self.app, user=self.viewer)
        owner = ApplicationGrant.objects.get(application=self.app, user=self.owner)
        self.assertNotIn("learned", [kind.key for kind in visible(self.app, viewer)])
        self.assertIn("learned", [kind.key for kind in visible(self.app, owner)])

    def test_what_waits_is_an_attention_item(self):
        from platform_core.lifecycle import attention

        self.delivered()
        learning.collect(self.app)
        owner = ApplicationGrant.objects.get(application=self.app, user=self.owner)
        viewer = ApplicationGrant.objects.get(application=self.app, user=self.viewer)
        texts = [item.text for item in attention(self.app, owner)]
        self.assertIn("1 thing learned from use waits for a decision", texts)
        self.assertEqual(attention(self.app, viewer), [])

    def test_the_maintenance_lane_collects_for_every_application(self):
        from platform_core.document_worker import maintenance_steps

        self.assertIn(learning.collect_if_due, maintenance_steps())
        self.delivered()
        learning._last_collect = None
        self.assertTrue(learning.collect_if_due())
        self.assertFalse(learning.collect_if_due())
        self.assertEqual(LearningCandidate.objects.count(), 1)
