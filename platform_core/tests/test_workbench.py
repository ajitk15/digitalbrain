import json
import subprocess
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from platform_core.ai import answer_with_ai
from platform_core.connectors import ConnectorForm, sync_issues
from platform_core.models import (
    AIConfiguration,
    AIUsage,
    ApplicationGrant,
    ChangePlan,
    ChatTurn,
    Connector,
    Document,
    FeatureSwitch,
    KnowledgeEntry,
    OrganizationMember,
    User,
)
from platform_core.processing import process_document
from platform_core.workbench import add_knowledge, review_plan
from scripts.extract_text import extract

from . import test_documents


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class WorkbenchTests(TestCase):
    upload = test_documents.DocumentTests.upload

    # Reuse the isolated app/user/filesystem fixture, without inheriting upload test methods.
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.approver = User.objects.create_user("reviewer")
        OrganizationMember.objects.create(
            user=self.approver, organization_id=self.app.organization_id
        )
        ApplicationGrant.objects.create(
            application=self.app, user=self.approver, role="contributor", can_approve=True
        )

    def source(self):
        return add_knowledge(
            self.owner, self.app.pk, "Refund policy", "Refund requests expire after thirty days."
        )

    def submit_plan(self):
        response = self.client.post(
            reverse("plans", args=[self.app.pk]),
            {
                "title": "Change refund deadline",
                "proposal": "Update the deadline to forty days.",
                "validation": "Test days 39 and 41; roll back on failure.",
            },
        )
        self.assertEqual(response.status_code, 302)
        return ChangePlan.objects.get()

    def test_knowledge_create_search_archive_and_escape(self):
        entry = self.source()
        entry.content = "<script>alert('bad')</script>"
        entry.save()
        response = self.client.get(reverse("knowledge-detail", args=[self.app.pk, entry.pk]))
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "<script>alert")
        self.assertContains(
            self.client.get(reverse("knowledge", args=[self.app.pk]), {"q": "Refund"}),
            "Refund policy",
        )
        self.client.post(reverse("knowledge-detail", args=[self.app.pk, entry.pk]))
        self.assertEqual(
            self.client.get(reverse("knowledge-detail", args=[self.app.pk, entry.pk])).status_code,
            404,
        )

    def test_viewer_cannot_write_knowledge(self):
        self.client.force_login(self.viewer, backend="django.contrib.auth.backends.ModelBackend")
        self.assertEqual(
            self.client.post(
                reverse("knowledge", args=[self.app.pk]), {"title": "Bad", "content": "Bad"}
            ).status_code,
            403,
        )
        self.assertFalse(KnowledgeEntry.objects.exists())

    def test_chat_cites_only_this_application_and_keeps_history_private(self):
        self.source()
        ApplicationGrant.objects.create(application=self.other, user=self.owner, role="owner")
        add_knowledge(self.owner, self.other.pk, "Secret policy", "Refund deadline is secret.")
        response = self.client.post(
            reverse("chat", args=[self.app.pk]),
            {"question": "What is the refund deadline?", "mode": "search"},
        )
        self.assertEqual(response.status_code, 302)
        turn = ChatTurn.objects.get()
        self.assertEqual(turn.citations[0]["title"], "Refund policy")
        self.assertNotIn("Secret policy", json.dumps(turn.citations))
        self.client.force_login(self.viewer, backend="django.contrib.auth.backends.ModelBackend")
        self.assertNotContains(
            self.client.get(reverse("chat", args=[self.app.pk])), "What is the refund deadline?"
        )

    def test_chat_abstains_without_evidence_and_respects_feature_flags(self):
        self.client.post(
            reverse("chat", args=[self.app.pk]), {"question": "unknown topic", "mode": "search"}
        )
        self.assertIn("No matching evidence", ChatTurn.objects.get().answer)
        FeatureSwitch.objects.create(key="knowledge", enabled=False)
        self.assertEqual(self.client.get(reverse("chat", args=[self.app.pk])).status_code, 403)

    def test_all_feature_routes_deny_unassigned_application(self):
        for route in ["knowledge", "chat", "plans", "connectors", "ai-settings"]:
            self.assertEqual(self.client.get(reverse(route, args=[self.other.pk])).status_code, 404)
        self.client.force_login(self.admin, backend="django.contrib.auth.backends.ModelBackend")
        for route in ["knowledge", "chat", "plans", "connectors", "ai-settings"]:
            self.assertEqual(self.client.get(reverse(route, args=[self.app.pk])).status_code, 404)

    def test_plan_requires_independent_approver_and_cannot_be_reviewed_twice(self):
        self.source()
        plan = self.submit_plan()
        ApplicationGrant.objects.filter(application=self.app, user=self.owner).update(
            can_approve=True
        )
        with self.assertRaises(PermissionDenied):
            review_plan(self.owner, self.app.pk, plan.pk, "approved", "Self approval")
        with self.assertRaises(PermissionDenied):
            review_plan(self.viewer, self.app.pk, plan.pk, "approved", "No authority")
        review_plan(self.approver, self.app.pk, plan.pk, "approved", "Tests cover the boundaries.")
        with self.assertRaises(ValidationError):
            review_plan(self.approver, self.app.pk, plan.pk, "rejected", "Second decision")
        response = self.client.get(
            reverse("plan-detail", args=[self.app.pk, plan.pk]), {"download": "1"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)["digest"], plan.digest)

    def test_archived_source_prevents_plan_approval(self):
        entry = self.source()
        plan = self.submit_plan()
        entry.active = False
        entry.save()
        with self.assertRaises(ValidationError):
            review_plan(self.approver, self.app.pk, plan.pk, "approved", "Review complete")

    def test_tree_highlights_current_path_and_hides_unassigned_apps(self):
        response = self.client.get(reverse("knowledge", args=[self.app.pk]))
        self.assertContains(response, 'aria-label="Organization tree"')
        self.assertContains(response, 'aria-label="Application menu"')
        self.assertContains(
            response, f'href="{reverse("application", args=[self.app.pk])}" aria-current'
        )
        self.assertNotContains(response, f'href="{reverse("application", args=[self.other.pk])}"')
        html = response.content.decode()
        aside = html.split("<aside")[1].split("</aside>")[0]
        self.assertNotIn(">Audit log<", aside)
        self.assertNotIn(">Code Factory<", aside)
        self.assertContains(response, ">Code Factory</a>")

    def test_connector_validation_and_owner_only_configuration(self):
        for repo in ["https://evil.test/x", "../x", "owner/..", "owner/repo?query=x"]:
            self.assertFalse(ConnectorForm({"repository": repo}).is_valid())
        self.client.force_login(self.viewer, backend="django.contrib.auth.backends.ModelBackend")
        self.assertEqual(
            self.client.get(reverse("connectors", args=[self.app.pk])).status_code, 403
        )

    @patch("platform_core.connectors.read_secret", return_value="test-key")
    @patch("platform_core.connectors.read_issues")
    def test_connector_import_is_idempotent_and_versions_updates(self, read, secret):
        Connector.objects.create(application=self.app, repository="example/repo")
        read.return_value = [
            {"number": 1, "title": "Refund feature", "body": "Initial requirement"}
        ]
        self.assertEqual(sync_issues(self.owner, self.app.pk), 1)
        self.assertEqual(sync_issues(self.owner, self.app.pk), 0)
        read.return_value[0]["body"] = "Changed requirement"
        self.assertEqual(sync_issues(self.owner, self.app.pk), 1)
        self.assertEqual(KnowledgeEntry.objects.count(), 2)
        self.assertEqual(KnowledgeEntry.objects.filter(active=True).count(), 1)
        secret.assert_called_with(
            __import__("django.conf", fromlist=["settings"]).settings.SECRET_DIRECTORY,
            f"github_{self.app.pk}",
        )

    @patch("platform_core.processing.scanner_path", return_value="test-scanner")
    @patch("platform_core.processing.subprocess.run")
    @override_settings(DOCUMENT_SCAN_REQUIRED=True)
    def test_clean_scan_processes_document_once(self, run, scanner):
        self.upload()
        doc = Document.objects.get()
        run.side_effect = [
            subprocess.CompletedProcess([], 0),
            subprocess.CompletedProcess([], 0, stdout=b"Refund policy", stderr=b""),
        ]
        first = process_document(self.owner, self.app.pk, doc.pk)
        second = process_document(self.owner, self.app.pk, doc.pk)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(KnowledgeEntry.objects.count(), 1)
        doc.refresh_from_db()
        self.assertEqual(doc.status, "ready")

    @patch("platform_core.processing.scanner_path", return_value="test-scanner")
    @patch("platform_core.processing.subprocess.run")
    @override_settings(DOCUMENT_SCAN_REQUIRED=True)
    def test_infected_file_is_rejected_without_parsing(self, run, scanner):
        self.upload()
        doc = Document.objects.get()
        run.return_value = subprocess.CompletedProcess([], 1)
        with self.assertRaises(ValidationError):
            process_document(self.owner, self.app.pk, doc.pk)
        self.assertEqual(run.call_count, 1)
        self.assertFalse(KnowledgeEntry.objects.exists())
        doc.refresh_from_db()
        self.assertEqual(doc.status, "rejected")

    @patch("platform_core.processing.scanner_path", return_value="test-scanner")
    @patch("platform_core.processing.subprocess.run")
    @override_settings(DOCUMENT_SCAN_REQUIRED=True)
    def test_scanner_errors_keep_file_quarantined(self, run, scanner):
        self.upload()
        doc = Document.objects.get()
        run.return_value = subprocess.CompletedProcess([], 2)
        with self.assertRaises(ValidationError):
            process_document(self.owner, self.app.pk, doc.pk)
        doc.refresh_from_db()
        self.assertEqual(doc.status, "quarantined")
        self.assertFalse(KnowledgeEntry.objects.exists())

    def test_text_and_docx_extraction(self):
        import zipfile

        root = Path(self.directory.name)
        text = root / "a.txt"
        text.write_text("Refund policy", encoding="utf-8")
        self.assertEqual(extract(text, ".txt"), "Refund policy")
        doc = root / "a.docx"
        with zipfile.ZipFile(doc, "w") as archive:
            archive.writestr(
                "word/document.xml",
                (
                    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                    "<w:body><w:p><w:r><w:t>Refund policy</w:t></w:r></w:p></w:body></w:document>"
                ),
            )
        self.assertIn("Refund policy", extract(doc, ".docx"))

    def test_pdf_extraction_rejects_empty_pdf(self):
        from pypdf import PdfWriter

        path = Path(self.directory.name) / "empty.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.write(path)
        with self.assertRaises(ValueError):
            extract(path, ".pdf")

    @patch("platform_core.ai.read_secret", return_value="test-key")
    @patch("platform_core.ai.completion")
    def test_ai_usage_is_recorded_even_with_reports_disabled(self, completion, secret):
        AIConfiguration.objects.create(
            application=self.app,
            model="configured-model",
            input_rate=Decimal("2"),
            output_rate=Decimal("6"),
        )
        FeatureSwitch.objects.create(key="usage_reports", enabled=False)
        completion.return_value = (
            {"id": "response-1", "usage": {"prompt_tokens": 100, "completion_tokens": 50}},
            "Refund requests expire after thirty days [1].",
        )
        answer = answer_with_ai(
            self.owner,
            self.app.pk,
            "Refund deadline?",
            [{"title": "Policy", "excerpt": "Thirty days", "id": "source"}],
        )
        self.assertIn("[1]", answer)
        receipt = AIUsage.objects.get()
        self.assertEqual(receipt.amount, Decimal("0.0005"))
        self.assertTrue(receipt.estimated)
        self.assertEqual(receipt.application, self.app)

    @patch("platform_core.ai.completion")
    def test_missing_ai_configuration_never_calls_provider(self, completion):
        with self.assertRaises(ValidationError):
            answer_with_ai(self.owner, self.app.pk, "Question", [{"excerpt": "Evidence"}])
        completion.assert_not_called()

    def test_all_new_pages_render(self):
        for name in ["knowledge", "chat", "plans", "connectors", "ai-settings"]:
            self.assertEqual(self.client.get(reverse(name, args=[self.app.pk])).status_code, 200)
