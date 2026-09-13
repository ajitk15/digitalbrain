from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from platform_core.graph_ai import (
    available_graph_versions,
    graph_citations,
    graph_snapshot,
)
from platform_core.models import ChatConversation, GraphRevision
from platform_core.workbench import add_knowledge

from . import test_documents


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class GraphVersionSelectionTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.source = add_knowledge(
            self.owner, self.app.pk, "Architecture", "Service Alpha sends messages to Queue Beta."
        )

    def revision(self, number, relation="sends messages to"):
        """A saved snapshot whose single edge quotes the live source exactly."""
        quote = "Service Alpha sends messages to Queue Beta."
        data = {
            "nodes": [
                {"id": "a", "label": "Service Alpha", "kind": "entity"},
                {"id": "b", "label": "Queue Beta", "kind": "entity"},
            ],
            "edges": [
                {
                    "source": "a",
                    "target": "b",
                    "relation": relation,
                    "knowledge_id": str(self.source.pk),
                    "digest": self.source.digest,
                    "evidence": quote,
                    "line": 1,
                    "inferred": True,
                }
            ],
            "sources": [{"id": str(self.source.pk)}],
        }
        return GraphRevision.objects.create(
            application=self.app, number=number, fingerprint="f", data=data, quality={}
        )

    def test_available_versions_are_listed_newest_first(self):
        self.revision(1)
        self.revision(2)
        self.assertEqual(available_graph_versions(self.app.pk), [2, 1])

    def test_a_chosen_version_is_answered_from_instead_of_the_live_graph(self):
        self.revision(1, relation="sends messages to")
        self.revision(2, relation="publishes to")
        data, number = graph_snapshot(self.app.pk, version=1)
        self.assertEqual(number, 1)
        self.assertEqual(data["edges"][0]["relation"], "sends messages to")
        citations = graph_citations(self.app.pk, "Service Alpha", version=1)
        self.assertEqual(citations[0]["graph_version"], "1")
        self.assertIn("sends messages to", citations[0]["excerpt"])

    def test_an_unknown_version_is_refused(self):
        self.revision(1)
        with self.assertRaises(ValidationError):
            graph_snapshot(self.app.pk, version=99)

    def test_a_version_belonging_to_another_application_is_refused(self):
        self.revision(1)
        with self.assertRaises(ValidationError):
            graph_snapshot(self.other.pk, version=1)

    def test_a_version_whose_evidence_changed_yields_no_citations(self):
        """Picking an old version skips the fingerprint check but not verification."""
        self.revision(1)
        self.source.active = False
        self.source.save(update_fields=["active"])
        self.assertEqual(graph_citations(self.app.pk, "Service Alpha", version=1), [])

    def test_a_conversation_remembers_its_pinned_version(self):
        self.revision(1)
        self.revision(2)
        conversation = ChatConversation.objects.create(
            application=self.app, user=self.owner, title="Graph questions", mode="graph"
        )
        url = reverse("chat-conversation", args=[self.app.pk, conversation.pk])
        self.client.post(url, {"action": "graph-version", "graph_version": "1"})
        conversation.refresh_from_db()
        self.assertEqual(conversation.graph_version, 1)
        # Back to the live graph.
        self.client.post(url, {"action": "graph-version", "graph_version": ""})
        conversation.refresh_from_db()
        self.assertIsNone(conversation.graph_version)

    def test_an_unavailable_version_cannot_be_pinned(self):
        self.revision(1)
        conversation = ChatConversation.objects.create(
            application=self.app, user=self.owner, title="Graph questions", mode="graph"
        )
        url = reverse("chat-conversation", args=[self.app.pk, conversation.pk])
        for value in ["99", "not-a-number", "-1"]:
            with self.subTest(value=value):
                self.client.post(url, {"action": "graph-version", "graph_version": value})
                conversation.refresh_from_db()
                self.assertIsNone(conversation.graph_version)

    def test_the_version_selector_appears_for_graph_conversations(self):
        self.revision(1)
        from platform_core.models import AIConfiguration

        AIConfiguration.objects.create(
            application=self.app,
            purpose="graph_retrieval",
            provider="claude",
            model="claude-sonnet-5",
            enabled=True,
            input_rate=Decimal("2"),
            output_rate=Decimal("10"),
            configured_by=self.owner,
        )
        conversation = ChatConversation.objects.create(
            application=self.app, user=self.owner, title="Graph questions", mode="graph"
        )
        response = self.client.get(
            reverse("chat", args=[self.app.pk]), {"conversation": conversation.pk}
        )
        self.assertContains(response, 'name="graph_version"')
        self.assertContains(response, "Version 1")


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class GraphPublishingTests(TestCase):
    """Generation produces drafts; only a published version answers questions."""

    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.source = add_knowledge(
            self.owner, self.app.pk, "Architecture", "Service Alpha sends messages to Queue Beta."
        )
        self.url = reverse("graph", args=[self.app.pk])

    def revision(self, number):
        quote = "Service Alpha sends messages to Queue Beta."
        return GraphRevision.objects.create(
            application=self.app,
            number=number,
            fingerprint="f",
            data={
                "nodes": [
                    {"id": "a", "label": "Service Alpha", "kind": "entity"},
                    {"id": "b", "label": "Queue Beta", "kind": "entity"},
                ],
                "edges": [
                    {
                        "source": "a",
                        "target": "b",
                        "relation": "sends messages to",
                        "knowledge_id": str(self.source.pk),
                        "digest": self.source.digest,
                        "evidence": quote,
                        "line": 1,
                        "inferred": True,
                    }
                ],
                "sources": [{"id": str(self.source.pk), "digest": self.source.digest}],
            },
            quality={},
        )

    def test_nothing_is_answered_from_until_something_is_published(self):
        self.revision(1)
        with self.assertRaises(ValidationError) as raised:
            graph_snapshot(self.app.pk)
        self.assertIn("published", str(raised.exception))

    def test_publishing_makes_a_version_the_default_answer(self):
        self.revision(1)
        self.revision(2)
        self.client.post(self.url, {"action": "publish", "version": "1"})
        _, number = graph_snapshot(self.app.pk)
        self.assertEqual(number, 1)
        # A later publish supersedes it.
        self.client.post(self.url, {"action": "publish", "version": "2"})
        _, number = graph_snapshot(self.app.pk)
        self.assertEqual(number, 2)

    def test_a_draft_never_becomes_the_answer_on_its_own(self):
        self.client.post(self.url, {"action": "publish", "version": str(self.revision(1).number)})
        self.revision(2)
        _, number = graph_snapshot(self.app.pk)
        self.assertEqual(number, 1)

    def test_a_version_with_changed_evidence_cannot_be_published(self):
        self.revision(1)
        self.source.active = False
        self.source.save(update_fields=["active"])
        response = self.client.post(self.url, {"action": "publish", "version": "1"})
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(GraphRevision.objects.get(number=1).published_at)

    def test_publishing_an_unknown_version_is_rejected(self):
        for value in ["99", "abc", ""]:
            with self.subTest(value=value):
                response = self.client.post(self.url, {"action": "publish", "version": value})
                self.assertIn(response.status_code, {302, 400})
        self.assertFalse(GraphRevision.objects.filter(published_at__isnull=False).exists())

    def test_a_viewer_cannot_publish(self):
        self.revision(1)
        self.client.force_login(self.viewer, backend="django.contrib.auth.backends.ModelBackend")
        self.assertEqual(
            self.client.post(self.url, {"action": "publish", "version": "1"}).status_code, 403
        )

    def test_generation_records_the_chosen_model_on_the_request(self):
        from platform_core.models import AIConfiguration, KnowledgeGraph

        AIConfiguration.objects.create(
            application=self.app,
            purpose="graph_generation",
            provider="claude",
            model="claude-haiku-4-5-20251001",
            enabled=True,
            input_rate=Decimal("2"),
            output_rate=Decimal("10"),
            configured_by=self.owner,
        )
        self.client.post(
            self.url, {"action": "generate", "model_choice": "claude:claude-sonnet-5"}
        )
        graph = KnowledgeGraph.objects.get(application=self.app)
        self.assertEqual(graph.status, "queued")
        # The per-run choice overrides the saved model without changing it.
        self.assertEqual(graph.requested_model, "claude-sonnet-5")
        self.assertEqual(AIConfiguration.objects.get().model, "claude-haiku-4-5-20251001")

    def test_a_structural_run_requests_no_model_and_cannot_be_billed(self):
        from platform_core.models import KnowledgeGraph

        self.client.post(self.url, {"action": "generate", "model_choice": "structural"})
        graph = KnowledgeGraph.objects.get(application=self.app)
        self.assertEqual(graph.status, "queued")
        self.assertEqual(graph.requested_model, "")

    def test_an_unknown_model_is_refused(self):
        from platform_core.models import KnowledgeGraph

        self.client.post(self.url, {"action": "generate", "model_choice": "evil:model"})
        self.assertFalse(
            KnowledgeGraph.objects.filter(application=self.app)
            .exclude(requested_model="")
            .exists()
        )
