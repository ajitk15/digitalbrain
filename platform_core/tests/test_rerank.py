"""Reranking reorders verified evidence and can do nothing else."""

import json
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from platform_core import rerank
from platform_core.graph_ai import graph_citations
from platform_core.graphs import build_graph, sources_for
from platform_core.models import AIConfiguration, AIUsage, BenchmarkQuestion, GraphRevision
from platform_core.workbench import add_knowledge, lexical_citations

from . import test_documents


class ParseTests(SimpleTestCase):
    def test_numbers_become_positions_and_nonsense_is_dropped(self):
        self.assertEqual(rerank.parse('{"order": [3, 1, 3, 9, true, "2"]}', 4), [2, 0])
        self.assertEqual(rerank.parse('```json\n{"order": [2]}\n```', 2), [1])
        self.assertIsNone(rerank.parse("first one", 3))
        self.assertIsNone(rerank.parse('{"order": 2}', 3))


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class RerankTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.entries = [
            add_knowledge(self.owner, self.app.pk, f"Doc {n}", f"Consent export note {n}.")
            for n in range(4)
        ]
        entries = list(sources_for(self.app.pk))
        data, quality = build_graph(entries, len(entries))
        GraphRevision.objects.create(
            application=self.app,
            number=1,
            fingerprint="x",
            data=data,
            quality=quality,
            published_at=timezone.now(),
        )
        self.config = AIConfiguration.objects.create(
            application=self.app,
            purpose="reranking",
            provider="claude",
            model="claude-haiku-4-5-20251001",
            input_rate=Decimal("1"),
            output_rate=Decimal("5"),
            configured_by=self.owner,
        )
        patcher = patch("platform_core.secrets.application_secret", return_value="key")
        patcher.start()
        self.addCleanup(patcher.stop)

    def reply(self, order):
        return (
            {"id": f"req-{order}", "usage": {"prompt_tokens": 50, "completion_tokens": 5}},
            json.dumps({"order": order}),
        )

    def ask(self, **kwargs):
        return graph_citations(self.app.pk, "consent export note", **kwargs)

    def test_the_model_reorders_and_the_rest_keep_their_order(self):
        plain = self.ask()
        with patch("platform_core.claude_agents.completion", return_value=self.reply([3, 1])):
            ranked = self.ask(user=self.owner)
        ids = [c["id"] for c in ranked]
        self.assertEqual(ids[0], plain[2]["id"])
        self.assertEqual(ids[1], plain[0]["id"])
        self.assertEqual(set(ids), {c["id"] for c in plain})
        receipt = AIUsage.objects.get()
        self.assertEqual(receipt.purpose, "reranking")

    def test_it_cannot_add_or_change_evidence(self):
        plain = self.ask()
        with patch("platform_core.claude_agents.completion", return_value=self.reply([99, 1])):
            ranked = self.ask(user=self.owner)
        self.assertEqual([c["id"] for c in ranked], [c["id"] for c in plain])
        self.assertEqual(ranked[0]["quote"], plain[0]["quote"])

    def test_any_failure_keeps_the_original_order(self):
        plain = self.ask()
        with patch("platform_core.claude_agents.completion", return_value=self.reply("x")):
            self.assertEqual(self.ask(user=self.owner), plain)
        with patch(
            "platform_core.claude_agents.completion",
            side_effect=ValidationError("down"),
        ):
            with self.assertLogs("platform_core.rerank", "WARNING"):
                self.assertEqual(self.ask(user=self.owner), plain)

    def test_off_or_without_a_user_nothing_is_called(self):
        with patch("platform_core.claude_agents.completion") as call:
            self.ask()
            AIConfiguration.objects.filter(pk=self.config.pk).update(enabled=False)
            self.ask(user=self.owner)
        call.assert_not_called()

    def test_keyword_search_is_reranked_too(self):
        app = self.app
        with patch("platform_core.claude_agents.completion", return_value=self.reply([2])):
            ranked = lexical_citations(app, "consent export note", self.owner)
        plain = lexical_citations(app, "consent export note")
        self.assertEqual(ranked[0]["id"], plain[1]["id"])

    def test_a_graph_answer_without_javascript_runs_no_keyword_search(self):
        from platform_core.workbench import answer_question

        with (
            patch("platform_core.workbench.lexical_citations",
                  side_effect=AssertionError("keyword search in a graph answer")),
            patch("platform_core.ai.invoke_ai", return_value="Answer [1]") as invoked,
        ):
            conversation = answer_question(self.owner, self.app, None,
                                           "consent export note", "graph")
        reply = conversation.messages.get(role="assistant")
        self.assertTrue(reply.citations)
        self.assertTrue(all(c["excerpt"].startswith("Graph v1:") for c in reply.citations))
        purposes = [call.args[2] for call in invoked.call_args_list]
        self.assertEqual(purposes[:2], ["reranking", "graph_retrieval"])
        self.assertEqual(purposes.count("reranking"), 1)

    def test_the_benchmark_measures_it_only_when_asked_and_paid_for(self):
        BenchmarkQuestion.objects.create(
            application=self.app, question="consent export note", expected="Doc 3"
        )
        with patch("platform_core.claude_agents.completion") as call:
            call_command("benchmark", str(self.app.pk), stdout=StringIO())
        call.assert_not_called()
        with self.assertRaises(CommandError):
            call_command("benchmark", str(self.app.pk), "--rerank", stdout=StringIO())
        out = StringIO()
        with patch("platform_core.claude_agents.completion", return_value=self.reply([4])):
            call_command("benchmark", str(self.app.pk), "--rerank", "--as", "owner", stdout=out)
        self.assertIn("(reranked)", out.getvalue())
