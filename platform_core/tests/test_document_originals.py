"""Conversion reports what it saw; a deployment may keep or drop the original."""

import hashlib
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from digitalbrain import configuration
from platform_core.document_worker import process_next_document
from platform_core.models import (
    ApplicationGrant,
    AuditEvent,
    Document,
    KnowledgeEntry,
    OrganizationMember,
    User,
)
from platform_core.processing import convert_document

from . import test_documents

PDF = Path(settings.BASE_DIR) / "demo-artifacts/docs/01-requirements/regulatory-requirements.pdf"

SETTINGS = dict(
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    DOCUMENT_AUTO_CONVERT=True,
    DOCUMENT_SCAN_REQUIRED=False,
)


def with_blank_page(path):
    """The PDF's bytes with an empty page added at the end: a scan with no text layer."""
    from io import BytesIO

    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for page in PdfReader(path).pages:
        writer.add_page(page)
    writer.add_blank_page()
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


class ReportTests(SimpleTestCase):
    def test_a_pdf_reports_its_pages_and_those_with_no_text(self):
        from scripts.extract_text import extract_with_report

        _, report = extract_with_report(PDF, ".pdf")
        self.assertGreater(report["pages"], 0)
        self.assertEqual(report["pages_without_text"], [])

    def test_a_page_with_no_text_layer_is_counted(self):
        import tempfile

        from scripts.extract_text import extract_with_report

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "scan.pdf"
            path.write_bytes(with_blank_page(PDF))
            text, report = extract_with_report(path, ".pdf")
        self.assertTrue(text.strip())
        self.assertEqual(report["pages_without_text"], [report["pages"]])

    def test_the_setting_must_be_a_boolean(self):
        import tempfile

        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "local.toml"
            config.write_text('mode = "development"\nkeep_original_documents = "no"\n')
            with patch.dict("os.environ", {"DIGITAL_BRAIN_CONFIG": str(config)}):
                with self.assertRaisesMessage(ImproperlyConfigured, "must be a boolean"):
                    configuration.load_config()
            config.write_text('mode = "production"\nkeep_original_documents = false\n')
            with patch.dict("os.environ", {"DIGITAL_BRAIN_CONFIG": str(config)}):
                try:
                    configuration.load_config()
                except ImproperlyConfigured as refused:
                    self.assertNotIn("keep_original_documents", str(refused))


@override_settings(**SETTINGS)
class OriginalTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)

    def upload(self, name, content):
        self.client.post(
            reverse("documents", args=[self.app.pk]),
            {"file": [SimpleUploadedFile(name, content)]},
        )
        return Document.objects.get(name=name)

    def stored(self, document):
        return (
            Path(self.directory.name) / ".runtime/documents" / str(self.app.organization_id)
            / str(self.app.pk) / f"{document.pk}.quarantine"
        )

    def convert(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.assertTrue(process_next_document())

    def test_a_converted_pdf_keeps_its_report_and_the_check_reads_it(self):
        document = self.upload("scan.pdf", with_blank_page(PDF))
        self.convert()
        document.refresh_from_db()
        self.assertEqual(document.status, "ready")
        self.assertEqual(document.conversion_report["pages_without_text"],
                         [document.conversion_report["pages"]])
        entry = KnowledgeEntry.objects.get(document=document)
        readable = next(c for c in entry.quality["checks"] if c["key"] == "readable")
        self.assertIn("had no text", readable["finding"])
        page = self.client.get(reverse("document-detail", args=[self.app.pk, document.pk]))
        self.assertContains(page, "1 with no text")

    def test_the_original_is_kept_by_default(self):
        document = self.upload("notes.csv", b"name,value\nnode,1\n")
        self.convert()
        document.refresh_from_db()
        self.assertTrue(self.stored(document).exists())
        self.assertIsNone(document.original_removed_at)

    @override_settings(KEEP_ORIGINAL_DOCUMENTS=False)
    def test_not_keeping_originals_removes_the_file_once_the_text_is_stored(self):
        document = self.upload("notes.csv", b"name,value\nnode,1\n")
        self.convert()
        document.refresh_from_db()
        self.assertEqual(document.status, "ready")
        self.assertFalse(self.stored(document).exists())
        self.assertIsNotNone(document.original_removed_at)
        # What was converted is still on record.
        self.assertEqual(document.sha256, hashlib.sha256(b"name,value\nnode,1\n").hexdigest())
        self.assertTrue(KnowledgeEntry.objects.filter(document=document, active=True).exists())
        page = self.client.get(reverse("document-detail", args=[self.app.pk, document.pk]))
        self.assertContains(page, "Removed after conversion")
        self.assertContains(page, "upload the file again")
        self.assertNotContains(page, "Convert again")

    @override_settings(KEEP_ORIGINAL_DOCUMENTS=False)
    def test_a_failed_conversion_keeps_its_file_so_retry_works(self):
        document = self.upload("notes.custom", b"source")
        with patch("platform_core.processing.process_document",
                   side_effect=ValidationError("Unsupported")):
            with self.captureOnCommitCallbacks(execute=True):
                with self.assertRaises(ValidationError):
                    convert_document(self.owner, self.app.pk, document.pk)
        document.refresh_from_db()
        self.assertEqual(document.status, "failed")
        self.assertTrue(self.stored(document).exists())
        self.client.post(reverse("document-detail", args=[self.app.pk, document.pk]))
        document.refresh_from_db()
        self.assertEqual(document.status, "queued")

    def test_converting_again_supersedes_and_keeps_a_restriction(self):
        document = self.upload("notes.csv", b"name,value\nnode,1\n")
        self.convert()
        first = KnowledgeEntry.objects.get(document=document)
        KnowledgeEntry.objects.filter(pk=first.pk).update(
            sensitivity="restricted", content="An older converter's text."
        )
        url = reverse("document-detail", args=[self.app.pk, document.pk])
        self.assertContains(self.client.get(url), "Convert again")
        self.client.post(url, {"action": "reconvert"})
        document.refresh_from_db()
        self.assertEqual(document.status, "queued")
        # Still answering from the current text while it waits.
        self.assertTrue(KnowledgeEntry.objects.get(pk=first.pk).active)
        self.convert()
        first.refresh_from_db()
        second = KnowledgeEntry.objects.get(document=document)
        self.assertNotEqual(second.pk, first.pk)
        self.assertEqual((first.active, first.document_id), (False, None))
        self.assertEqual(second.sensitivity, "restricted")
        self.assertIn("| node", second.content)
        self.assertTrue(AuditEvent.objects.filter(action="document.reconversion_queued").exists())

    def test_converting_again_to_the_same_text_changes_nothing(self):
        document = self.upload("notes.csv", b"name,value\nnode,1\n")
        self.convert()
        first = KnowledgeEntry.objects.get(document=document)
        self.client.post(reverse("document-detail", args=[self.app.pk, document.pk]),
                         {"action": "reconvert"})
        self.convert()
        self.assertEqual(KnowledgeEntry.objects.get(document=document).pk, first.pk)

    def test_a_failed_reconversion_keeps_the_text_it_had(self):
        document = self.upload("notes.csv", b"name,value\nnode,1\n")
        self.convert()
        self.client.post(reverse("document-detail", args=[self.app.pk, document.pk]),
                         {"action": "reconvert"})
        with patch("platform_core.processing.process_document",
                   side_effect=ValidationError("Unsupported")):
            self.convert()
        document.refresh_from_db()
        self.assertEqual(document.status, "ready")
        self.assertIn("Converting again failed: Unsupported", document.conversion_error)
        self.assertTrue(KnowledgeEntry.objects.filter(document=document, active=True).exists())

    def test_only_an_owner_converts_again(self):

        document = self.upload("notes.csv", b"name,value\nnode,1\n")
        self.convert()
        writer = User.objects.create_user("writer")
        OrganizationMember.objects.create(
            organization=self.app.product.portfolio.organization, user=writer
        )
        ApplicationGrant.objects.create(application=self.app, user=writer, role="contributor")
        self.client.force_login(writer, backend="django.contrib.auth.backends.ModelBackend")
        url = reverse("document-detail", args=[self.app.pk, document.pk])
        self.assertNotContains(self.client.get(url), "Convert again")
        self.assertEqual(self.client.post(url, {"action": "reconvert"}).status_code, 403)
        document.refresh_from_db()
        self.assertEqual(document.status, "ready")
