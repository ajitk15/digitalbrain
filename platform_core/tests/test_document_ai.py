"""AI help with document types: opt-in, budgeted, quoted, and never above the rules."""

import hashlib
import json
from decimal import Decimal
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from platform_core import document_ai, source_quality
from platform_core.ai import SEEDED_DISABLED
from platform_core.models import (
    AIBudget,
    AIConfiguration,
    AIUsage,
    AuditEvent,
    Document,
    DocumentAIResult,
    KnowledgeEntry,
)
from platform_core.processing import convert_document
from platform_core.workbench import add_knowledge

from . import test_documents
from .test_document_types import BRD

#: A document no rule can type: no title words, sections or columns of any kind.
NOTES = """Pharmacy pilot, ward 7

The pilot ran for six weeks across two wards. Barcode checks were added at the
point of dispensing, and every nurse on both wards was shown how to use them.

Dispensing errors fell from 14 to 3 over the six weeks. Two of the three that
remained were on night shifts, when the scanner on ward 7 was being charged.

Nurses said the checks took longer at first and about the same by week four.
"""
QUOTE = "Dispensing errors fell from 14 to 3 over the six weeks."


def typed(key="review", quotes=(QUOTE,)):
    return json.dumps({"type": key, "quotes": list(quotes)})


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class DocumentAITests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        patcher = patch("platform_core.secrets.application_secret", return_value="key")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls = 0

    def configure(self, purpose, enabled=True):
        return AIConfiguration.objects.update_or_create(
            application=self.app,
            purpose=purpose,
            defaults={
                "provider": "claude",
                "model": "claude-sonnet-5",
                "input_rate": Decimal("2"),
                "output_rate": Decimal("10"),
                "configured_by": self.owner,
                "enabled": enabled,
            },
        )[0]

    def document(self, content=NOTES, name="notes.docx"):
        document = Document.objects.create(
            application=self.app, uploaded_by=self.owner, name=name, size=len(content),
            sha256=hashlib.sha256(content.encode()).hexdigest(), status="ready",
            origin="upload",
        )
        return add_knowledge(self.owner, self.app.pk, name, content, document=document)

    def model(self, *answers):
        """The provider adapter, so `invoke_ai` - grants, budget, receipt - runs for real."""
        replies = list(answers)

        def reply(config, question, citations, token, **options):
            self.calls += 1
            self.seen = (question, citations, options)
            return (
                {"id": f"req-{self.calls}", "usage": {"prompt_tokens": 900,
                                                      "completion_tokens": 40}},
                replies.pop(0),
            )

        return patch("platform_core.claude_agents.completion", side_effect=reply)

    # ── the typing fallback ──────────────────────────────────────────────────

    def test_it_is_seeded_off_and_never_asked_while_off(self):
        self.assertIn("document_typing", SEEDED_DISABLED)
        self.assertNotIn("document_review", SEEDED_DISABLED)
        entry = self.document()
        with self.model(typed()) as call:
            self.assertIsNone(document_ai.classify(self.owner, entry))
            self.configure("document_typing", enabled=False)
            self.assertIsNone(document_ai.classify(self.owner, entry))
        call.assert_not_called()
        entry.refresh_from_db()
        self.assertEqual(entry.quality["type"]["key"], "other")

    def test_it_is_never_asked_when_the_rules_are_confident(self):
        self.configure("document_typing")
        entry = self.document(BRD, "upload.docx")
        self.assertNotEqual(entry.quality["type"]["confidence"], "low")
        with self.model(typed()) as call:
            self.assertIsNone(document_ai.classify(self.owner, entry))
        call.assert_not_called()

    def test_it_is_never_asked_when_a_person_set_the_type(self):
        self.configure("document_typing")
        entry = self.document()
        KnowledgeEntry.objects.filter(pk=entry.pk).update(document_type="policy")
        entry.refresh_from_db()
        with self.model(typed()) as call:
            self.assertIsNone(document_ai.classify(self.owner, entry))
        call.assert_not_called()

    def test_a_restricted_source_is_never_sent(self):
        self.configure("document_typing")
        entry = self.document()
        KnowledgeEntry.objects.filter(pk=entry.pk).update(sensitivity="restricted")
        entry.refresh_from_db()
        with self.model(typed()) as call:
            self.assertIsNone(document_ai.classify(self.owner, entry))
        call.assert_not_called()

    def test_a_quoted_type_is_stored_as_ai_and_judged_as_that_type(self):
        self.configure("document_typing")
        entry = self.document()
        with self.model(typed()):
            verdict = document_ai.classify(self.owner, entry)
        self.assertEqual((verdict["verdict"], verdict["key"]), ("accepted", "review"))
        self.assertEqual(verdict["quotes"], [{"text": QUOTE, "line": 6}])
        entry.refresh_from_db()
        kind = entry.quality["type"]
        self.assertEqual((kind["key"], kind["source"]), ("review", "ai"))
        self.assertEqual(kind["detected"]["key"], "other")
        self.assertTrue(entry.quality["type_checks"])
        # Paid, receipted and budgeted like any other call; the model read the text.
        receipt = AIUsage.objects.get()
        self.assertEqual(receipt.purpose, "document_typing")
        question, citations, options = self.seen
        self.assertEqual(citations[0]["excerpt"], NOTES)
        self.assertIn("user_stories", options["instructions"])

    def test_an_unverified_quote_leaves_it_other(self):
        self.configure("document_typing")
        entry = self.document()
        # One good quote does not carry one the document does not contain.
        with self.model(typed(quotes=[QUOTE, "Dispensing errors fell to zero."])):
            verdict = document_ai.classify(self.owner, entry)
        self.assertEqual((verdict["key"], verdict["reason"]), ("other", "quote not in document"))
        self.assertEqual(verdict["proposed"], "review")
        entry.refresh_from_db()
        self.assertEqual((entry.quality["type"]["key"], entry.quality["type"]["source"]),
                         ("other", "rules"))
        self.assertEqual(entry.quality["type_checks"], [])

    def test_a_type_outside_the_registry_or_no_quote_leaves_it_other(self):
        for answer, reason in (
            (typed(key="meeting_minutes"), "type outside the registry"),
            (typed(key="other"), "model said Other"),
            (typed(quotes=[]), "no quote"),
            (typed(quotes=["ward 7"]), "quote not in document"),  # too short to show anything
            ("I think it is a review.", "reply could not be read"),
        ):
            self.assertEqual(document_ai.judge_type(document_ai.parse(answer), NOTES)["reason"],
                             reason)

    def test_the_same_text_is_never_paid_for_twice(self):
        self.configure("document_typing")
        first = self.document()
        with self.model(typed()):
            document_ai.classify(self.owner, first)
        # The same text uploaded again, and the check worked out again from stale.
        second = self.document(name="copy.docx")
        self.assertEqual(second.quality["type"]["source"], "ai")
        with self.model() as call:
            document_ai.classify(self.owner, second)
        call.assert_not_called()
        KnowledgeEntry.objects.filter(pk=first.pk).update(quality={})
        first.refresh_from_db()
        self.assertEqual(source_quality.for_entries([first])[first.pk]["type"]["key"], "review")
        self.assertEqual(AIUsage.objects.count(), 1)
        self.assertEqual(DocumentAIResult.objects.count(), 1)

    def test_a_refused_verdict_is_kept_too(self):
        self.configure("document_typing")
        entry = self.document()
        with self.model(typed(key="memo")):
            document_ai.classify(self.owner, entry)
        with self.model() as call:
            verdict = document_ai.classify(self.owner, entry)
        call.assert_not_called()
        self.assertEqual(verdict["reason"], "type outside the registry")

    def test_a_persons_type_still_wins(self):
        self.configure("document_typing")
        entry = self.document()
        with self.model(typed()):
            document_ai.classify(self.owner, entry)
        response = self.client.post(reverse("knowledge-detail", args=[self.app.pk, entry.pk]),
                                    {"action": "set_type", "document_type": "postmortem"})
        self.assertEqual(response.status_code, 302)
        entry.refresh_from_db()
        self.assertEqual((entry.quality["type"]["key"], entry.quality["type"]["source"]),
                         ("postmortem", "person"))
        # Handed back to the rules, the kept AI verdict applies again, unpaid.
        self.client.post(reverse("knowledge-detail", args=[self.app.pk, entry.pk]),
                         {"action": "set_type", "document_type": ""})
        entry.refresh_from_db()
        self.assertEqual(entry.quality["type"]["source"], "ai")
        self.assertEqual(AIUsage.objects.count(), 1)

    def test_a_quote_no_longer_in_the_text_is_not_used(self):
        verdict = {"key": "review", "verdict": "accepted", "quotes": [{"text": QUOTE, "line": 6}]}
        kind = source_quality.kind_of(NOTES, "notes.docx", "", verdict)
        self.assertEqual(kind["source"], "ai")
        edited = NOTES.replace("14 to 3", "14 to 4")
        kind = source_quality.kind_of(edited, "notes.docx", "", verdict)
        self.assertEqual((kind["key"], kind["source"]), ("other", "rules"))

    def test_a_reached_budget_refuses_before_any_call(self):
        self.configure("document_typing")
        AIBudget.objects.create(application=self.app, monthly_limit=Decimal("1"))
        AIUsage.objects.create(application=self.app, actor=self.owner, provider="Anthropic",
                               model="claude-sonnet-5", request_id="spent", input_tokens=1,
                               output_tokens=1, amount=Decimal("1"), estimated=True)
        entry = self.document()
        with self.model(typed()) as call:
            self.assertIsNone(document_ai.classify_quietly(self.owner, entry))
            response = self.client.post(
                reverse("knowledge-detail", args=[self.app.pk, entry.pk]),
                {"action": "ai_type", "next": "document"}, follow=True,
            )
        call.assert_not_called()
        self.assertContains(response, "used its AI budget")
        self.assertFalse(DocumentAIResult.objects.exists())
        entry.refresh_from_db()
        self.assertEqual(entry.quality["type"]["key"], "other")

    def test_conversion_asks_once_the_text_is_stored(self):
        entry = self.document()
        with patch("platform_core.processing.process_document", return_value=entry), \
                patch("platform_core.document_ai.classify", return_value=None) as asked:
            self.assertEqual(convert_document(self.owner, self.app.pk, entry.document_id), entry)
        asked.assert_called_once_with(self.owner, entry)

    def test_the_page_offers_the_button_and_shows_the_quotes(self):
        self.configure("document_typing")
        entry = self.document()
        url = reverse("document-detail", args=[self.app.pk, entry.document_id])
        self.assertContains(self.client.get(url), 'value="ai_type"')
        with self.model(typed()):
            response = self.client.post(
                reverse("knowledge-detail", args=[self.app.pk, entry.pk]),
                {"action": "ai_type", "next": "document"},
            )
        self.assertRedirects(response, url)
        page = self.client.get(url)
        self.assertContains(page, "Named by AI (claude-sonnet-5)")
        self.assertContains(page, f"<q>{QUOTE}</q>", html=False)
        self.assertNotContains(page, 'value="ai_type"')
        event = AuditEvent.objects.get(action="knowledge.ai_type")
        self.assertEqual(event.details, {"verdict": "accepted", "type": "review"})

    def test_a_viewer_cannot_ask(self):
        self.configure("document_typing")
        entry = self.document()
        self.client.force_login(self.viewer, backend="django.contrib.auth.backends.ModelBackend")
        url = reverse("knowledge-detail", args=[self.app.pk, entry.pk])
        self.assertNotContains(self.client.get(url), 'value="ai_type"')
        with self.model(typed()) as call:
            response = self.client.post(url, {"action": "ai_type"})
        self.assertIn(response.status_code, {403, 404})
        call.assert_not_called()

    # ── the content review ───────────────────────────────────────────────────

    def review_answer(self):
        return json.dumps({"findings": [
            {"aspect": "testability", "quote": "| BR-01 | Referrals are tracked to closure",
             "finding": "Closure is not defined.", "suggestion": "Say what closes a referral."},
            {"aspect": "gap", "quote": "Referrals are lost forever.",
             "finding": "Invented.", "suggestion": ""},
            {"aspect": "tone", "quote": "## Scope", "finding": "Odd.", "suggestion": ""},
        ]})

    def test_a_review_keeps_only_findings_quoted_from_the_document(self):
        self.configure("document_review")
        entry = self.document(BRD, "upload.docx")
        before = source_quality.for_entries([entry])[entry.pk]["content"]["score"]
        with self.model(self.review_answer()):
            found = document_ai.review(self.owner, entry)
        self.assertEqual([f["aspect"] for f in found["findings"]], ["testability"])
        row = next(n for n, line in enumerate(BRD.splitlines(), 1) if line.startswith("| BR-01"))
        self.assertEqual(found["findings"][0]["line"], row)
        self.assertEqual(found["dropped_reasons"], {"quote not in document": 1, "malformed": 1})
        self.assertEqual(found["type"], "business_requirements")
        _, _, options = self.seen
        self.assertIn("business requirements document", options["instructions"])
        # Advisory: the rule-based content check is untouched.
        entry.refresh_from_db()
        self.assertEqual(source_quality.for_entries([entry])[entry.pk]["content"]["score"],
                         before)
        self.assertEqual(AIUsage.objects.get().purpose, "document_review")

    def test_the_owner_button_shows_the_review_beside_the_content_check(self):
        self.configure("document_review")
        entry = self.document(BRD, "upload.docx")
        url = reverse("document-detail", args=[self.app.pk, entry.document_id])
        page = self.client.get(url)
        self.assertContains(page, 'id="ai-review"')
        self.assertContains(page, 'value="ai_review"')
        with self.model(self.review_answer()):
            self.client.post(reverse("knowledge-detail", args=[self.app.pk, entry.pk]),
                             {"action": "ai_review", "next": "document"})
        page = self.client.get(url)
        content = page.content.decode()
        self.assertLess(content.index('id="content-check"'), content.index('id="ai-review"'))
        self.assertContains(page, "Closure is not defined.")
        self.assertNotContains(page, "Invented.")
        self.assertNotContains(page, 'value="ai_review"')
        # Pressed again for the same text: kept, not paid for.
        with self.model() as call:
            self.client.post(reverse("knowledge-detail", args=[self.app.pk, entry.pk]),
                             {"action": "ai_review"})
        call.assert_not_called()
        self.assertEqual(AIUsage.objects.count(), 1)

    def test_only_an_owner_may_ask_for_a_review(self):
        self.configure("document_review")
        entry = self.document(BRD, "upload.docx")
        from platform_core.models import ApplicationGrant, OrganizationMember, User

        contributor = User.objects.create_user("contributor")
        OrganizationMember.objects.create(organization=self.app.product.portfolio.organization,
                                          user=contributor)
        ApplicationGrant.objects.create(application=self.app, user=contributor,
                                        role="contributor")
        self.client.force_login(contributor, backend="django.contrib.auth.backends.ModelBackend")
        url = reverse("knowledge-detail", args=[self.app.pk, entry.pk])
        self.assertNotContains(self.client.get(url), 'value="ai_review"')
        with self.model(self.review_answer()) as call:
            response = self.client.post(url, {"action": "ai_review"})
        self.assertEqual(response.status_code, 403)
        call.assert_not_called()

    def test_a_review_respects_the_budget(self):
        self.configure("document_review")
        AIBudget.objects.create(application=self.app, monthly_limit=Decimal("0"))
        entry = self.document(BRD, "upload.docx")
        with self.model(self.review_answer()) as call:
            response = self.client.post(
                reverse("knowledge-detail", args=[self.app.pk, entry.pk]),
                {"action": "ai_review", "next": "document"}, follow=True,
            )
        call.assert_not_called()
        self.assertContains(response, "used its AI budget")
        self.assertFalse(DocumentAIResult.objects.exists())

    def test_a_review_needs_the_purpose_switched_on(self):
        entry = self.document(BRD, "upload.docx")
        page = self.client.get(reverse("document-detail", args=[self.app.pk, entry.document_id]))
        self.assertContains(page, "Switch on Document review")
        with self.model(self.review_answer()) as call:
            response = self.client.post(
                reverse("knowledge-detail", args=[self.app.pk, entry.pk]),
                {"action": "ai_review", "next": "document"}, follow=True,
            )
        call.assert_not_called()
        self.assertContains(response, "must configure Document review first")


class JudgeReviewTests(SimpleTestCase):
    def test_findings_beyond_the_limit_and_repeats_are_dropped(self):
        item = {"aspect": "clarity", "quote": QUOTE, "finding": "Which wards?", "suggestion": ""}
        payload = {"findings": [item] * 2 + [
            {**item, "quote": f"{QUOTE[:-1]}"}
        ] + [{**item, "aspect": "gap"}] * (document_ai.MAX_FINDINGS)}
        kept, dropped = document_ai.judge_review(payload, NOTES)
        self.assertEqual(len(kept), 3)
        self.assertEqual(dropped["repeated"], document_ai.MAX_FINDINGS - 3)
        self.assertEqual(dropped["over the limit"], 3)

    def test_an_unreadable_reply_is_said(self):
        kept, dropped = document_ai.judge_review(None, NOTES)
        self.assertEqual((kept, dropped[document_ai.UNREADABLE]), ([], 1))


class TypingPackTests(SimpleTestCase):
    """The frozen pack stays what it says it is, with no model called."""

    def test_every_item_is_one_the_rules_leave_as_other(self):
        from platform_core import document_types, typing_pack

        pack = typing_pack.load_pack()
        self.assertEqual(pack["registry_version"], document_types.REGISTRY_VERSION)
        labels = {item["label"] for item in pack["items"]}
        self.assertIn("other", labels)
        self.assertGreater(len(labels), 3)
        for item in pack["items"]:
            self.assertIn(item["label"], document_types.BY_KEY, item["id"])
            self.assertTrue(
                document_ai.low_confidence(document_types.classify(item["text"])), item["id"]
            )

    def test_the_score_counts_a_type_named_for_other_against_it(self):
        from platform_core.typing_pack import score

        accepted = {"verdict": "accepted", "key": "runbook"}
        refused = {"verdict": "refused", "key": "other", "reason": "quote not in document"}
        metrics = score([("runbook", accepted), ("postmortem", refused), ("other", accepted),
                         ("postmortem", accepted)])
        self.assertEqual((metrics["recall"], metrics["precision"]), (33.3, 33.3))
        self.assertEqual((metrics["wrong_type"], metrics["false_positive_on_other"],
                          metrics["quote_refused"]), (1, 1, 1))
