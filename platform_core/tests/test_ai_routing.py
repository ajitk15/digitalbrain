import json
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from platform_core.ai import AIForm, invoke_ai
from platform_core.graph_ai import graph_citations
from platform_core.graphs import fingerprint, process_next_graph, rebuild
from platform_core.models import (
    AIConfiguration,
    AIUsage,
    ApplicationGrant,
    ChatMessage,
    GraphRevision,
    KnowledgeGraph,
)
from platform_core.workbench import add_knowledge

from . import test_documents


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class AIRoutingTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.source = add_knowledge(
            self.owner, self.app.pk, "Architecture", "Service Alpha sends messages to Queue Beta."
        )

    def ai_config(self, purpose="chat", provider="claude"):
        return AIConfiguration.objects.create(
            application=self.app,
            purpose=purpose,
            provider=provider,
            model="claude-sonnet-5" if provider == "claude" else "gpt-5.6-luna",
            input_rate=Decimal("2"),
            output_rate=Decimal("10"),
            configured_by=self.owner,
        )

    def request_enrichment(self, model="claude-sonnet-5", provider="claude"):
        """Enrichment is opt-in: a run has to be asked for, with a chosen model.

        This mirrors queue_generation, including status="queued" - that status is
        what marks the run as explicitly requested, and only a requested run is
        allowed to spend money.
        """
        graph, _ = KnowledgeGraph.objects.get_or_create(application=self.app)
        KnowledgeGraph.objects.filter(pk=graph.pk).update(
            status="queued",
            fingerprint="",
            requested_provider=provider,
            requested_model=model,
            requested_by=self.owner,
        )
        graph.refresh_from_db()
        return graph

    def publish_latest(self):
        from platform_core.graphs import publish_revision

        newest = GraphRevision.objects.filter(application=self.app).order_by("-number").first()
        return publish_revision(self.owner, self.app.pk, newest.number)

    def result(self, answer="A readable answer [1]."):
        return {
            "id": "test-request",
            "usage": {"prompt_tokens": 100, "completion_tokens": 10},
        }, answer

    def test_settings_show_both_providers_and_three_independent_tasks(self):
        response = self.client.get(reverse("ai-settings", args=[self.app.pk]))
        for label in ["GPT-5.6 Luna", "Claude Sonnet 5", "Graph generation", "Graph retrieval"]:
            self.assertContains(response, label)
        url = reverse("ai-settings", args=[self.app.pk])
        for purpose in ["chat", "graph_generation", "graph_retrieval"]:
            values = {
                "model_choice": "claude:claude-sonnet-5",
                "provider": "openai",
                "model": "",
                "enabled": "on",
                "input_rate": "2",
                "output_rate": "10",
            }
            response = self.client.post(
                url, {"purpose": purpose, **{f"{purpose}-{k}": v for k, v in values.items()}}
            )
            self.assertEqual(
                response.status_code, 302, response.context and response.context["sections"]
            )
        self.assertEqual(AIConfiguration.objects.filter(application=self.app).count(), 3)
        self.assertEqual(
            set(AIConfiguration.objects.values_list("provider", flat=True)), {"claude"}
        )
        self.client.force_login(self.viewer, backend="django.contrib.auth.backends.ModelBackend")
        self.assertEqual(self.client.post(url, {"purpose": "chat"}).status_code, 403)

    def test_custom_provider_mismatch_is_rejected(self):
        form = AIForm(
            {
                "model_choice": "custom",
                "provider": "openai",
                "model": "claude-sonnet-5",
                "enabled": "on",
                "input_rate": "2",
                "output_rate": "10",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("model", form.errors)

    @patch("platform_core.secrets.application_secret", return_value="scoped-key")
    @patch("platform_core.claude_agents.completion")
    @patch("platform_core.ai.completion")
    def test_purpose_routes_provider_credentials_and_cost(self, openai, claude, secret):
        self.ai_config("chat", "openai")
        self.ai_config("graph_retrieval", "claude")
        openai.return_value = self.result()
        claude.return_value = self.result()
        citations = [
            {"id": str(self.source.pk), "title": self.source.title, "excerpt": self.source.content}
        ]
        invoke_ai(self.owner, self.app.pk, "graph_retrieval", "Queue?", citations)
        openai.assert_not_called()
        # The credential fetched is this application's, for the provider the
        # purpose routed to - not the one configured for chat.
        secret.assert_called_with(self.app, "claude")
        receipt = AIUsage.objects.get()
        self.assertEqual(receipt.provider, "Anthropic")
        self.assertEqual(receipt.purpose, "graph_retrieval")
        self.assertEqual(receipt.amount, Decimal("0.0003"))

    @patch("platform_core.secrets.application_secret", return_value="scoped-key")
    @patch("platform_core.claude_agents.completion")
    def test_graph_generation_uses_model_and_preserves_source_provenance(self, claude, secret):
        self.ai_config("graph_generation")
        claude.return_value = self.result(
            json.dumps(
                {
                    "relationships": [
                        {
                            "subject": "Service Alpha",
                            "relation": "sends messages to",
                            "object": "Queue Beta",
                            "source_id": str(self.source.pk),
                            "quote": self.source.content,
                        },
                        {
                            "subject": "Invented",
                            "relation": "owns",
                            "object": "Queue Beta",
                            "source_id": str(self.source.pk),
                            "quote": "invented evidence",
                        },
                    ]
                }
            )
        )
        self.request_enrichment()
        rebuild(self.app.pk)
        graph = KnowledgeGraph.objects.get(application=self.app)
        self.assertEqual(graph.quality["semantic_relationships"], 1)
        self.assertEqual(graph.quality["rejected_relationships"], 1)
        self.assertEqual(graph.quality["model"], "claude-sonnet-5")
        self.assertTrue(any(e.get("inferred") for e in graph.data["edges"]))
        self.assertEqual(AIUsage.objects.get().purpose, "graph_generation")
        # The chosen model is recorded on the snapshot it produced.
        snapshot = GraphRevision.objects.filter(application=self.app).order_by("-number").first()
        self.assertEqual(snapshot.model, "claude-sonnet-5")
        self.assertEqual(snapshot.provider, "claude")
        self.assertIsNone(snapshot.published_at)
        old_version = graph.version
        # A rebuild that nobody asked for must not repeat a paid call.
        rebuild(self.app.pk)
        self.assertEqual(claude.call_count, 1)
        self.assertEqual(KnowledgeGraph.objects.get().version, old_version)
        config = AIConfiguration.objects.get()
        config.input_rate = Decimal("3")
        config.save()
        self.assertEqual(fingerprint(self.app.pk), graph.fingerprint)
        config.model = "claude-opus-5"
        config.save()
        self.assertNotEqual(fingerprint(self.app.pk), graph.fingerprint)

    @patch("platform_core.secrets.application_secret", return_value="scoped-key")
    @patch("platform_core.claude_agents.completion", side_effect=ValidationError("Unavailable"))
    def test_failed_generation_does_not_repeat_paid_calls_automatically(self, claude, secret):
        self.ai_config("graph_generation")
        self.request_enrichment()
        with self.assertRaises(ValidationError):
            process_next_graph()
        self.assertFalse(process_next_graph())
        self.assertEqual(claude.call_count, 1)
        self.assertEqual(KnowledgeGraph.objects.get().status, "failed")
        response = self.client.post(reverse("graph", args=[self.app.pk]), {"action": "retry"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(KnowledgeGraph.objects.get().status, "queued")

    @patch("platform_core.secrets.application_secret", return_value="scoped-key")
    @patch("platform_core.claude_agents.completion")
    def test_graph_answers_use_saved_edges_and_private_history(self, claude, secret):
        self.ai_config("graph_retrieval")
        self.request_enrichment()
        rebuild(self.app.pk)
        self.publish_latest()
        claude.return_value = self.result()
        response = self.client.post(
            reverse("chat", args=[self.app.pk]), {"question": "Service Alpha", "mode": "graph"}
        )
        self.assertEqual(response.status_code, 302)
        answer = ChatMessage.objects.get(role="assistant")
        self.assertEqual(answer.mode, "graph")
        self.assertIn("graph_version", answer.citations[0])
        self.assertEqual(claude.call_args.args[0].purpose, "graph_retrieval")
        # A published snapshot is pinned, so changed evidence no longer errors: each
        # edge simply fails verification and the answer has nothing to cite.
        self.source.active = False
        self.source.save()
        self.assertEqual(graph_citations(self.app.pk, "Service Alpha"), [])
        # An application with nothing published still refuses outright.
        ApplicationGrant.objects.create(application=self.other, user=self.owner, role="owner")
        with self.assertRaises(ValidationError):
            graph_citations(self.other.pk, "Service Alpha")

    def test_unconfigured_chat_is_explicit_and_never_falls_back_to_search(self):
        url = reverse("chat", args=[self.app.pk])
        page = self.client.get(url)
        self.assertContains(page, "LLM chat is not configured")
        self.assertContains(page, 'value="ai" selected')
        response = self.client.post(url, {"question": "Hello there"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "must configure Chat conversation")
        self.assertFalse(ChatMessage.objects.exists())

    @patch("platform_core.secrets.application_secret", return_value="scoped-key")
    @patch("platform_core.ai.completion")
    def test_default_chat_calls_llm_for_greetings_without_document_matches(self, provider, secret):
        self.ai_config("chat", "openai")
        provider.return_value = self.result("Hi! What would you like to explore today?")
        response = self.client.post(reverse("chat", args=[self.app.pk]), {"question": "Hi!"})
        self.assertEqual(response.status_code, 302)
        provider.assert_called_once()
        self.assertEqual(provider.call_args.args[3], "scoped-key")
        self.assertEqual(provider.call_args.args[2], [])
        answer = ChatMessage.objects.get(role="assistant")
        self.assertEqual(answer.mode, "ai")
        self.assertIn("Hi!", answer.body)
        self.assertEqual(AIUsage.objects.get().purpose, "chat")

    def test_enabled_model_without_key_shows_setup_and_retains_draft(self):
        self.ai_config("chat", "openai")

        with patch("platform_core.secrets.application_secret", return_value=""):
            response = self.client.post(reverse("chat", args=[self.app.pk]), {"question": "Hello"})
        self.assertContains(response, "API key file is missing")
        self.assertContains(response, "gpt-5.6-luna")
        self.assertContains(response, "Hello")
        self.assertFalse(ChatMessage.objects.exists())


class PricingReferenceTests(SimpleTestCase):
    """The 100x-too-high rates this fixes came from having no guidance at all."""

    def test_every_listed_claude_model_has_a_published_price(self):
        from platform_core.model_catalog import LIST_PRICES, MODEL_CHOICES

        claude = [v for group, options in MODEL_CHOICES for v, _ in options
                  if group == "Claude"]
        self.assertTrue(claude)
        for value in claude:
            with self.subTest(model=value):
                self.assertIn(value, LIST_PRICES)

    def test_output_always_costs_more_than_input(self):
        from platform_core.model_catalog import LIST_PRICES

        for value, (inputs, outputs) in LIST_PRICES.items():
            with self.subTest(model=value):
                self.assertLess(float(inputs), float(outputs))

    def test_openai_models_are_absent_rather_than_guessed(self):
        """Inventing a price would be worse than showing none."""
        from platform_core.model_catalog import LIST_PRICES

        self.assertFalse([v for v in LIST_PRICES if v.startswith("openai:")])

    def test_the_reference_renders_in_order_with_both_rates(self):
        from platform_core.model_catalog import price_reference

        rows = price_reference()
        self.assertEqual(rows[0]["label"], "Claude Fable 5.1")
        self.assertEqual(rows[-1]["label"], "Claude Haiku 4.5")
        self.assertEqual(rows[-1]["input"], "1.00")
        self.assertEqual(rows[-1]["output"], "5.00")
