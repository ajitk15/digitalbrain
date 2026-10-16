"""Search embeddings find starting points by meaning; citations are still verified.

No test calls OpenAI. The request itself runs through the real SDK over a
mocked HTTP transport; everything else replaces `embeddings.request` with a
deterministic stand-in that places texts by a few hand-picked concepts.
"""

import json
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

import httpx2
import numpy as np
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import CommandError, call_command
from django.http import Http404
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from openai import DefaultHttpxClient

from platform_core import embeddings
from platform_core.ai import AIForm, seed_application_ai
from platform_core.benchmark import run as run_benchmark
from platform_core.graph_ai import graph_citations
from platform_core.graphs import build_graph, rebuild, sources_for
from platform_core.models import (
    AIConfiguration,
    AIUsage,
    BenchmarkQuestion,
    Embedding,
    GraphRevision,
    KnowledgeEntry,
    KnowledgeGraph,
)
from platform_core.workbench import add_knowledge, retrieve

from . import test_documents

#: Concept -> words that carry it. Texts sharing a concept are near; others are not.
CONCEPTS = [
    ("consent", "said no", "refused", "opted out"),
    ("deploy", "rollback", "release"),
]
QUESTION = "Why can people who said no still be downloaded?"


def place(text):
    vector = np.zeros(embeddings.DIMENSIONS, dtype=np.float32)
    lowered = text.casefold()
    for position, words in enumerate(CONCEPTS):
        if any(word in lowered for word in words):
            vector[position] = 1.0
    vector[-1] = 0.05
    return vector / np.linalg.norm(vector)


class Fake:
    """Stands in for `embeddings.request`, counting calls and texts."""

    def __init__(self):
        self.calls = []

    def __call__(self, token, model, texts):
        self.calls.append(list(texts))
        return [place(text) for text in texts], f"req-{len(self.calls)}", 7 * len(texts)


class RequestTests(SimpleTestCase):
    """The one function that talks to OpenAI, through the real SDK."""

    def respond(self, handler):
        return patch(
            "platform_core.embeddings._http_client",
            side_effect=lambda: DefaultHttpxClient(
                transport=httpx2.MockTransport(handler), follow_redirects=False, trust_env=False
            ),
        )

    def body(self, count=1, size=embeddings.DIMENSIONS, usage=True):
        data = {
            "object": "list",
            "model": "text-embedding-3-small",
            "data": [
                {"object": "embedding", "index": index, "embedding": [0.5] * size}
                for index in range(count)
            ],
        }
        if usage:
            data["usage"] = {"prompt_tokens": 9, "total_tokens": 9}
        return data

    def test_the_request_is_scoped_and_the_vectors_are_unit_length(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx2.Response(200, json=self.body(2), headers={"x-request-id": "req-e"})

        with self.respond(handler):
            vectors, request_id, tokens = embeddings.request(
                "application-key", "text-embedding-3-small", ["a", "b"]
            )
        self.assertEqual((request_id, tokens, len(vectors)), ("req-e", 9, 2))
        self.assertAlmostEqual(float(np.linalg.norm(vectors[0])), 1.0, places=5)
        request = seen[0]
        self.assertEqual(str(request.url), "https://api.openai.com/v1/embeddings")
        self.assertEqual(request.headers["authorization"], "Bearer application-key")
        payload = json.loads(request.content)
        self.assertEqual(payload["dimensions"], embeddings.DIMENSIONS)
        self.assertEqual(payload["input"], ["a", "b"])

    def test_missing_usage_is_an_error_not_a_free_request(self):
        with self.respond(lambda request: httpx2.Response(200, json=self.body(usage=False))):
            with self.assertRaises(ValueError):
                embeddings.request("k", "text-embedding-3-small", ["a"])

    def test_a_vector_of_the_wrong_size_or_count_is_refused(self):
        with self.respond(lambda request: httpx2.Response(200, json=self.body(size=3))):
            with self.assertRaises(ValueError):
                embeddings.request("k", "text-embedding-3-small", ["a"])
        with self.respond(lambda request: httpx2.Response(200, json=self.body(count=1))):
            with self.assertRaises(ValueError):
                embeddings.request("k", "text-embedding-3-small", ["a", "b"])

    def test_fusion_keeps_the_word_order_when_meaning_adds_nothing(self):
        self.assertEqual(embeddings.fuse([3, 1, 2]), [3, 1, 2])
        self.assertEqual(embeddings.fuse([3, 1, 2], [2]), [2, 3, 1])


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class HybridTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        # Shares no word with QUESTION: only meaning can reach it.
        self.ticket = add_knowledge(
            self.owner, self.app.pk, "CARE-401", "Export ignores the consent flag."
        )
        # Shares two words with QUESTION and means something else.
        self.handbook = add_knowledge(
            self.owner, self.app.pk, "Handbook", "People can download the release notes."
        )
        self.config = AIConfiguration.objects.create(
            application=self.app,
            purpose="embeddings",
            provider="openai",
            model="text-embedding-3-small",
            input_rate=Decimal("0.02"),
            output_rate=Decimal("0"),
            configured_by=self.owner,
        )
        self.fake = Fake()
        for patcher in (
            patch("platform_core.embeddings.request", self.fake),
            patch("platform_core.secrets.application_secret", return_value="application-key"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def publish(self):
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
        return data, entries

    def index(self):
        data, entries = self.publish()
        return embeddings.index(self.owner, self.app, data, entries)

    def cited(self, **kwargs):
        return {c["id"] for c in graph_citations(self.app.pk, QUESTION, **kwargs)}

    def test_meaning_reaches_a_source_that_shares_no_word(self):
        data, entries = self.publish()
        self.assertNotIn(str(self.ticket.pk), self.cited())
        embeddings.index(self.owner, self.app, data, entries)
        self.assertIn(str(self.ticket.pk), self.cited(user=self.owner))

    def test_each_request_is_receipted_and_nothing_is_paid_for_twice(self):
        report = self.index()
        self.assertEqual(report["relationships"], report["relationships_total"])
        self.assertEqual(report["passages"], report["passages_total"])
        receipts = AIUsage.objects.filter(purpose="embeddings")
        self.assertEqual(receipts.count(), len(self.fake.calls))
        self.assertEqual(receipts.get().input_tokens, 7 * len(self.fake.calls[0]))
        self.assertEqual(receipts.get().provider, "OpenAI")
        calls = len(self.fake.calls)
        data, entries = (
            list(GraphRevision.objects.values_list("data", flat=True))[0],
            list(sources_for(self.app.pk)),
        )
        self.assertEqual(embeddings.index(self.owner, self.app, data, entries)["new"], 0)
        self.assertEqual(len(self.fake.calls), calls)

    def test_a_question_is_paid_for_once_and_never_without_someone_to_charge(self):
        self.index()
        calls = len(self.fake.calls)
        # Nobody to charge: words alone, and no request.
        self.assertNotIn(str(self.ticket.pk), self.cited())
        self.assertEqual(len(self.fake.calls), calls)
        self.cited(user=self.owner)
        self.assertEqual(len(self.fake.calls), calls + 1)
        # Stored now: free for anyone, including a caller with no user.
        self.assertIn(str(self.ticket.pk), self.cited())
        self.assertEqual(len(self.fake.calls), calls + 1)

    def test_switched_off_means_words_alone_and_no_spend(self):
        self.index()
        AIConfiguration.objects.filter(pk=self.config.pk).update(enabled=False)
        calls = len(self.fake.calls)
        self.assertNotIn(str(self.ticket.pk), self.cited(user=self.owner))
        self.assertEqual(len(self.fake.calls), calls)

    def test_a_failed_question_embedding_still_answers_by_words(self):
        self.index()
        with patch("platform_core.embeddings.request", side_effect=ValueError("down")):
            with self.assertLogs("platform_core.embeddings", "WARNING"):
                cited = graph_citations(self.app.pk, "Something new about notes?", user=self.owner)
        self.assertIn(str(self.handbook.pk), {c["id"] for c in cited})

    def test_a_vector_found_citation_is_verified_like_any_other(self):
        self.index()
        KnowledgeEntry.objects.filter(pk=self.ticket.pk).update(digest="0" * 64)
        self.assertNotIn(str(self.ticket.pk), self.cited(user=self.owner))

    def test_passages_by_meaning_are_re_read_and_skipped_once_changed(self):
        self.index()
        found = [entry.pk for _, entry, _ in retrieve(self.app, QUESTION, self.owner)]
        self.assertIn(self.ticket.pk, found)
        KnowledgeEntry.objects.filter(pk=self.ticket.pk).update(
            content="Export now refuses without the consent flag."
        )
        found = [entry.pk for _, entry, _ in retrieve(self.app, QUESTION, self.owner)]
        self.assertNotIn(self.ticket.pk, found)

    def test_another_applications_vectors_are_never_used(self):
        self.index()
        Embedding.objects.update(application=self.other)
        embeddings.forget(self.app.pk)
        calls = len(self.fake.calls)
        # The question is embedded for this application, and finds nothing of its own.
        self.assertNotIn(str(self.ticket.pk), self.cited(user=self.owner))
        self.assertEqual(len(self.fake.calls), calls + 1)

    def test_indexing_needs_write_access_and_the_feature_on(self):
        data, entries = self.publish()
        with self.assertRaises(PermissionDenied):
            embeddings.index(self.viewer, self.app, data, entries)
        with self.assertRaises(Http404):
            embeddings.index(self.owner, self.other, data, entries)
        AIConfiguration.objects.filter(pk=self.config.pk).update(enabled=False)
        with self.assertRaises(ValidationError):
            embeddings.index(self.owner, self.app, data, entries)
        self.assertFalse(AIUsage.objects.exists())

    def test_the_benchmark_uses_stored_question_vectors_and_spends_nothing(self):
        BenchmarkQuestion.objects.create(application=self.app, question=QUESTION, expected="CARE")
        self.index()
        calls = len(self.fake.calls)
        result = run_benchmark(self.app, GraphRevision.objects.get())
        self.assertEqual(len(self.fake.calls), calls)
        self.assertEqual(result.found, 1)
        self.assertTrue(result.results[0]["vector"])

    def test_a_requested_ai_run_embeds_and_a_background_rebuild_does_not(self):
        AIConfiguration.objects.create(
            application=self.app,
            purpose="graph_generation",
            provider="claude",
            model="claude-sonnet-5",
            input_rate=Decimal("2"),
            output_rate=Decimal("10"),
            configured_by=self.owner,
        )
        rebuild(self.app.pk)
        self.assertEqual(self.fake.calls, [])
        graph = KnowledgeGraph.objects.get(application=self.app)
        KnowledgeGraph.objects.filter(pk=graph.pk).update(
            status="queued",
            fingerprint="",
            requested_provider="claude",
            requested_model="claude-sonnet-5",
            requested_by=self.owner,
        )
        with patch("platform_core.ai.invoke_ai", return_value='{"relationships": []}'):
            rebuild(self.app.pk)
        quality = GraphRevision.objects.order_by("-number").first().quality
        self.assertEqual(quality["embeddings"]["model"], "text-embedding-3-small")
        self.assertGreater(quality["embeddings"]["relationships"], 0)

    def test_a_failed_embedding_never_costs_the_version(self):
        AIConfiguration.objects.create(
            application=self.app,
            purpose="graph_generation",
            provider="claude",
            model="claude-sonnet-5",
            input_rate=Decimal("2"),
            output_rate=Decimal("10"),
            configured_by=self.owner,
        )
        graph, _ = KnowledgeGraph.objects.get_or_create(application=self.app)
        KnowledgeGraph.objects.filter(pk=graph.pk).update(
            status="queued",
            fingerprint="",
            requested_provider="claude",
            requested_model="claude-sonnet-5",
            requested_by=self.owner,
        )
        with (
            patch("platform_core.ai.invoke_ai", return_value='{"relationships": []}'),
            patch("platform_core.embeddings.request", side_effect=ValueError("down")),
        ):
            rebuild(self.app.pk)
        revision = GraphRevision.objects.get()
        self.assertNotIn("embeddings", revision.quality)
        self.assertTrue(any("Embedding request failed" in w for w in revision.quality["warnings"]))

    def test_the_command_names_who_pays(self):
        self.publish()
        out = StringIO()
        call_command("embed", str(self.app.pk), "--as", "owner", stdout=out)
        self.assertIn("relationships", out.getvalue())
        self.assertEqual(AIUsage.objects.filter(actor=self.owner, purpose="embeddings").count(), 1)
        with self.assertRaises(CommandError):
            call_command("embed", str(self.app.pk), "--as", "viewer", stdout=StringIO())


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class SettingsTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)

    def form(self, **values):
        data = {
            "model_choice": "openai:text-embedding-3-small",
            "provider": "openai",
            "model": "",
            "enabled": "on",
            "input_rate": "0.02",
            "output_rate": "0",
            **values,
        }
        return AIForm(data, purpose="embeddings")

    def test_only_openai_embedding_models_are_accepted(self):
        self.assertTrue(self.form().is_valid())
        self.assertFalse(self.form(model_choice="custom", model="gpt-5.6-luna").is_valid())
        self.assertFalse(self.form(model_choice="claude:claude-sonnet-5").is_valid())
        self.assertFalse(self.form(model_choice="custom", provider="claude").is_valid())

    def test_the_settings_page_offers_and_saves_it(self):
        url = reverse("ai-settings", args=[self.app.pk])
        self.assertContains(self.client.get(url), "Search embeddings")
        values = {
            "model_choice": "openai:text-embedding-3-small",
            "provider": "openai",
            "model": "",
        }
        response = self.client.post(
            reverse("ai-task", args=[self.app.pk, "embeddings"]),
            {f"embeddings-{k}": v for k, v in values.items()},
        )
        self.assertEqual(response.status_code, 302)
        # Choosing its model does not switch it on: it spends on every question.
        self.assertIsNone(embeddings.configuration(self.app.pk))
        response = self.client.post(url, {"purpose": "embeddings", "enabled": "1"})
        self.assertEqual(response.status_code, 302)
        config = embeddings.configuration(self.app.pk)
        self.assertIsNotNone(config)
        self.assertEqual(config.output_rate, 0)

    def test_a_new_application_is_seeded_with_it_off(self):
        with patch("platform_core.ai.configured_providers", return_value=[]):
            with patch(
                "platform_core.agent_runtime.credentials.host_login_enabled", return_value=True
            ):
                seed_application_ai(self.owner, self.app)
        config = AIConfiguration.objects.get(application=self.app, purpose="embeddings")
        self.assertEqual((config.provider, config.enabled), ("openai", False))
        self.assertIsNone(embeddings.configuration(self.app.pk))
