"""Data-loss and boundary defects found in review, each reproduced before fixing.

1. Regenerate and edit deleted the exchange before re-asking, so a provider
   failure destroyed the question and every later message and returned nothing.
2. Naming a graph version bypassed publication: an API caller got a 200 from an
   unpublished draft, and the version picker offered drafts to pin.
3. A queued import kept the application's GitHub/SharePoint credential after the
   person who queued it lost access.
4. Bearer API answers carried no cache directive at all.
"""

import json
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from platform_core.api_auth import issue
from platform_core.graph_ai import available_graph_versions
from platform_core.models import (
    ApiToken,
    ApplicationGrant,
    ChatConversation,
    ChatMessage,
    Document,
    GraphRevision,
)
from platform_core.workbench import add_knowledge

from . import test_documents

SETTINGS = dict(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)


@override_settings(**SETTINGS)
class TranscriptPreservationTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.conversation = ChatConversation.objects.create(
            application=self.app, user=self.owner, title="T", mode="ai"
        )
        for index, (role, body) in enumerate(
            [("user", "Q1"), ("assistant", "A1"), ("user", "Q2"), ("assistant", "A2")], start=1
        ):
            ChatMessage.objects.create(
                application=self.app,
                user=self.owner,
                conversation=self.conversation,
                role=role,
                body=body,
                sequence=index,
                status="complete",
            )

    def transcript(self):
        return list(self.conversation.messages.order_by("sequence").values_list("body", flat=True))

    def failing(self):
        return patch(
            "platform_core.workbench.lexical_citations",
            side_effect=ValidationError("provider down"),
        )

    def test_a_failed_regenerate_keeps_the_exchange_it_was_replacing(self):
        answer = ChatMessage.objects.get(conversation=self.conversation, sequence=4)
        with self.failing():
            self.client.post(reverse("chat-regenerate", args=[self.app.pk, answer.pk]))
        self.assertEqual(self.transcript(), ["Q1", "A1", "Q2", "A2"])

    def test_a_failed_edit_keeps_the_question_it_was_replacing(self):
        original = ChatMessage.objects.get(conversation=self.conversation, sequence=3)
        with self.failing():
            self.client.post(
                reverse("chat-edit", args=[self.app.pk, original.pk]), {"question": "Q2 revised"}
            )
        self.assertEqual(self.transcript(), ["Q1", "A1", "Q2", "A2"])

    def test_a_successful_regenerate_still_replaces_the_old_exchange(self):
        """The trim must still happen; only the failure path was wrong.

        Search mode needs no provider, so this exercises the success path rather
        than quietly taking the failure path for want of an AI configuration.
        """
        add_knowledge(self.owner, self.app.pk, "Refunds", "Refunds expire after thirty days.")
        self.conversation.mode = "search"
        self.conversation.save(update_fields=["mode"])
        answer = ChatMessage.objects.get(conversation=self.conversation, sequence=4)
        self.client.post(reverse("chat-regenerate", args=[self.app.pk, answer.pk]))
        transcript = self.transcript()
        self.assertEqual(transcript[:2], ["Q1", "A1"])
        self.assertEqual(transcript.count("Q2"), 1, "re-asked, not duplicated")
        self.assertNotIn("A2", transcript, "the old answer goes once a new one exists")


@override_settings(**SETTINGS)
class PublicationBoundaryTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.source = add_knowledge(
            self.owner, self.app.pk, "Arch", "Service Alpha sends messages to Queue Beta."
        )
        self.revision(1, published=True)
        self.revision(2, published=False)
        prefix, self.secret, digest = issue()
        ApiToken.objects.create(
            application=self.app, user=self.owner, name="t", prefix=prefix, digest=digest
        )
        self.url = reverse("api-graph-search", args=[self.app.pk])

    def revision(self, number, published):
        quote = "Service Alpha sends messages to Queue Beta."
        return GraphRevision.objects.create(
            application=self.app,
            number=number,
            fingerprint=f"f{number}",
            published_at=timezone.now() if published else None,
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
                    }
                ],
                "sources": [{"id": str(self.source.pk), "digest": self.source.digest}],
            },
            quality={},
        )

    def call(self, **params):
        return self.client.get(
            self.url, params, headers={"Authorization": f"Bearer {self.secret}"}
        )

    def test_naming_an_unpublished_version_is_refused(self):
        """Pinning a version is not a way around review."""
        response = self.call(q="Service Alpha", version=2)
        self.assertEqual(response.status_code, 409)
        self.assertIn("not published", response.json()["error"]["message"])

    def test_naming_a_published_version_still_works(self):
        response = self.call(q="Service Alpha", version=1)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["version"], 1)

    def test_the_version_picker_offers_published_versions_only(self):
        self.assertEqual(available_graph_versions(self.app.pk), [1])

    def test_the_mcp_tool_is_bound_by_the_same_rule(self):
        result = self.client.post(
            reverse("api-mcp", args=[self.app.pk]),
            data=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "search_knowledge_graph",
                        "arguments": {"question": "Service Alpha", "version": 2},
                    },
                }
            ),
            content_type="application/json",
            headers={"Authorization": f"Bearer {self.secret}"},
        ).json()
        self.assertTrue(result["result"]["isError"])

    def test_private_api_answers_are_not_cacheable(self):
        self.assertEqual(self.call(q="Service Alpha")["Cache-Control"], "no-store")


@override_settings(**SETTINGS)
class QueuedImportAccessTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.document = Document.objects.create(
            application=self.app,
            name="readme.md",
            uploaded_by=self.owner,
            status="pending",
            origin="github",
            size=0,
            source_url="https://github.com/o/r/blob/main/README.md",
        )

    def test_a_revoked_uploader_cannot_still_spend_the_application_credential(self):
        from platform_core.link_sources import download

        ApplicationGrant.objects.filter(application=self.app, user=self.owner).delete()
        with patch("platform_core.link_sources.fetch") as fetched:
            download(self.document)
        fetched.assert_not_called()
        self.document.refresh_from_db()
        self.assertEqual(self.document.status, "failed")
        self.assertIn("no longer has access", self.document.conversion_error)

    def test_a_deactivated_uploader_is_stopped_too(self):
        from platform_core.link_sources import download

        self.owner.is_active = False
        self.owner.save(update_fields=["is_active"])
        with patch("platform_core.link_sources.fetch") as fetched:
            download(self.document)
        fetched.assert_not_called()

    def test_an_uploader_who_still_has_access_downloads_normally(self):
        from platform_core.link_sources import download

        with patch("platform_core.link_sources.fetch") as fetched:
            fetched.return_value = (b"# Readme", "text/markdown", "README.md", "https://x/")
            download(self.document)
        fetched.assert_called_once()
