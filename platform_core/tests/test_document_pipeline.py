import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from platform_core.document_worker import process_next_document
from platform_core.models import ApplicationGrant, AuditEvent, Document, KnowledgeEntry
from platform_core.processing import convert_document

from . import test_documents


@override_settings(
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    DOCUMENT_AUTO_CONVERT=True,
    DOCUMENT_SCAN_REQUIRED=False,
)
class PipelineTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)

    def post_files(self, *files):
        return self.client.post(
            reverse("documents", args=[self.app.pk]),
            {"file": [SimpleUploadedFile(name, content) for name, content in files]},
        )

    def test_multiple_uploads_queue_and_worker_builds_markdown_and_graph_input(self):
        value = b"name,value\nnode,1\n"
        response = self.post_files(("first.csv", value), ("second.csv", value))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Document.objects.filter(status="queued").count(), 2)
        with patch("platform_core.processing.scanner_path") as scanner:
            self.assertTrue(process_next_document())
            scanner.assert_not_called()
        doc = Document.objects.get(status="ready")
        entry = KnowledgeEntry.objects.get(document=doc)
        self.assertIn("| name", entry.content)
        self.assertIn("| node", entry.content)
        self.assertTrue(doc.converter.startswith("MarkItDown "))
        response = self.client.get(
            reverse("document-detail", args=[self.app.pk, doc.pk]), {"download": "graph"}
        )
        graph = json.loads(response.content)
        self.assertEqual(graph["markdown"], entry.content)
        self.assertEqual(
            graph["markdown_sha256"], hashlib.sha256(entry.content.encode()).hexdigest()
        )
        self.assertEqual(graph["application_id"], str(self.app.pk))
        self.assertEqual(graph["source_sha256"], hashlib.sha256(value).hexdigest())
        self.assertTrue(
            AuditEvent.objects.filter(
                action="document.processed", details__security_scan_required=False
            ).exists()
        )

    def test_large_csv_converts_without_the_old_100k_limit(self):
        from scripts.extract_text import extract

        path = Path(self.directory.name) / "large.csv"
        path.write_text("name,value\n" + "node,1234567890\n" * 28000, encoding="utf-8")
        markdown = extract(path, ".csv")
        self.assertGreater(len(markdown), 100000)
        self.assertIn("1234567890", markdown)

    def test_batch_is_validated_before_any_file_is_stored(self):
        response = self.post_files(("valid.csv", b"a,b"), ("empty.txt", b""))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Document.objects.exists())
        response = self.post_files(*[(f"{i}.txt", b"text") for i in range(21)])
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Document.objects.exists())

    def test_conversion_failure_is_visible_and_retryable(self):
        from django.core.exceptions import ValidationError

        self.post_files(("document.custom", b"source"))
        doc = Document.objects.get()
        with patch(
            "platform_core.processing.process_document", side_effect=ValidationError("Unsupported")
        ):
            with self.assertRaises(ValidationError):
                convert_document(self.owner, self.app.pk, doc.pk)
        doc.refresh_from_db()
        self.assertEqual(doc.status, "failed")
        self.assertIn("Unsupported", doc.conversion_error)
        self.client.post(reverse("document-detail", args=[self.app.pk, doc.pk]))
        doc.refresh_from_db()
        self.assertEqual(doc.status, "queued")

    def test_delete_confirmation_then_remove_original_knowledge_and_queue(self):
        self.post_files(("original.csv", b"a,b\n1,2"))
        doc = Document.objects.get()
        KnowledgeEntry.objects.create(
            application=self.app,
            author=self.owner,
            document=doc,
            title=doc.name,
            content="Markdown",
            digest=hashlib.sha256(b"Markdown").hexdigest(),
        )
        path = (
            Path(self.directory.name)
            / ".runtime/documents"
            / str(self.app.organization_id)
            / str(self.app.pk)
            / f"{doc.pk}.quarantine"
        )
        url = reverse("document-delete", args=[self.app.pk, doc.pk])
        self.assertContains(self.client.get(url), "Delete document?")
        self.assertTrue(path.exists())
        self.assertEqual(self.client.post(url).status_code, 302)
        self.assertFalse(path.exists())
        self.assertFalse(KnowledgeEntry.objects.filter(document=doc).exists())
        self.assertFalse(process_next_document())
        self.assertEqual(
            self.client.get(reverse("document-detail", args=[self.app.pk, doc.pk])).status_code, 404
        )
        self.assertTrue(AuditEvent.objects.filter(action="document.deleted").exists())

    def test_viewer_and_other_application_cannot_delete(self):
        self.post_files(("original.csv", b"a,b"))
        doc = Document.objects.get()
        self.client.force_login(self.viewer, backend="django.contrib.auth.backends.ModelBackend")
        self.assertEqual(
            self.client.post(reverse("document-delete", args=[self.app.pk, doc.pk])).status_code,
            403,
        )
        self.client.force_login(self.owner, backend="django.contrib.auth.backends.ModelBackend")
        ApplicationGrant.objects.create(application=self.other, user=self.owner, role="owner")
        self.assertEqual(
            self.client.post(reverse("document-delete", args=[self.other.pk, doc.pk])).status_code,
            404,
        )
        doc.refresh_from_db()
        self.assertEqual(doc.status, "queued")

    def test_deleted_file_cannot_be_revived_by_conversion_failure(self):
        self.post_files(("original.csv", b"a,b"))
        doc = Document.objects.get()
        Document.objects.filter(pk=doc.pk).update(status="deleted")
        with self.assertRaises(Document.DoesNotExist):
            convert_document(self.owner, self.app.pk, doc.pk)
        doc.refresh_from_db()
        self.assertEqual(doc.status, "deleted")
