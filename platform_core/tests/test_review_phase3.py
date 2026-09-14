"""Robustness defects from the review: bad input became 500s, and streams could
starve the very requests that would stop them."""

import json

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from platform_core.agent_runtime.streaming import MAX_ACTIVE_STREAMS
from platform_core.api_auth import issue
from platform_core.fetching import FetchError, normalise
from platform_core.models import ApiToken, Document, GraphRevision
from platform_core.workbench import add_knowledge

from . import test_documents

SETTINGS = dict(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)


class MalformedUrlTests(SimpleTestCase):
    """Every rejection has to arrive as FetchError, or it becomes a 500."""

    def test_malformed_addresses_are_reported_not_raised(self):
        for raw in (
            "http://[::1/",
            "http://example.com:99999/",
            "http://example.com:-1/",
            "http://example.com:notaport/",
        ):
            with self.subTest(url=raw):
                with self.assertRaises(FetchError):
                    normalise(raw)

    def test_a_valid_address_with_a_port_still_parses(self):
        self.assertEqual(normalise("https://example.com:8443/x").port, 8443)


@override_settings(**SETTINGS)
class ApiInputTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        source = add_knowledge(self.owner, self.app.pk, "Arch", "Alpha sends to Beta.")
        GraphRevision.objects.create(
            application=self.app,
            number=1,
            fingerprint="f",
            published_at=timezone.now(),
            data={"nodes": [], "edges": [], "sources": [{"id": str(source.pk)}]},
            quality={},
        )
        prefix, self.secret, digest = issue()
        ApiToken.objects.create(
            application=self.app, user=self.owner, name="t", prefix=prefix, digest=digest
        )
        self.url = reverse("api-graph-search", args=[self.app.pk])

    def post(self, body):
        return self.client.post(
            self.url,
            data=json.dumps(body),
            content_type="application/json",
            headers={"Authorization": f"Bearer {self.secret}"},
        )

    def test_a_question_of_the_wrong_type_is_a_client_error(self):
        """These reached .strip() and produced a 500."""
        for shape in (123, {"a": 1}, [1, 2], True):
            with self.subTest(shape=shape):
                response = self.post({"question": shape})
                self.assertEqual(response.status_code, 400)
                self.assertIn("must be a string", response.json()["error"]["message"])

    def test_a_version_of_the_wrong_type_is_a_client_error(self):
        for shape in ({"a": 1}, [1], "abc", True, 0, -3):
            with self.subTest(shape=shape):
                response = self.post({"question": "Alpha", "version": shape})
                self.assertEqual(response.status_code, 400)

    def test_a_well_formed_request_is_unaffected(self):
        self.assertEqual(self.post({"question": "Alpha"}).status_code, 200)


class StreamCapacityTests(SimpleTestCase):
    def test_request_threads_outnumber_the_streams_that_can_hold_them(self):
        """A stream occupies a request thread, so a full set must leave threads free."""
        import ast
        from pathlib import Path

        source = Path("scripts/serve.py").read_text(encoding="utf-8")
        self.assertIn("threads=REQUEST_THREADS", source)
        tree = ast.parse(source)
        multiplier = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and getattr(node.targets[0], "id", "") == "REQUEST_THREADS"
            ):
                multiplier = ast.literal_eval(node.value.right)
        self.assertIsNotNone(multiplier, "REQUEST_THREADS is no longer derived")
        self.assertGreater(MAX_ACTIVE_STREAMS * multiplier, MAX_ACTIVE_STREAMS)


@override_settings(**SETTINGS)
class FailedLinkRetryTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)

    def document(self, origin, size):
        return Document.objects.create(
            application=self.app,
            name="thing.md",
            uploaded_by=self.owner,
            status="failed",
            origin=origin,
            size=size,
            source_url="https://example.com/thing.md" if origin != "upload" else "",
        )

    def retry(self, document):
        self.client.post(reverse("document-detail", args=[self.app.pk, document.pk]))
        document.refresh_from_db()
        return document.status

    def test_a_link_that_never_downloaded_is_queued_to_download_again(self):
        """Queueing conversion only failed again on the file that was never stored."""
        self.assertEqual(self.retry(self.document("link", size=0)), "pending")

    def test_a_downloaded_link_is_queued_for_conversion(self):
        self.assertEqual(self.retry(self.document("github", size=120)), "queued")

    def test_an_uploaded_file_is_always_queued_for_conversion(self):
        self.assertEqual(self.retry(self.document("upload", size=0)), "queued")
