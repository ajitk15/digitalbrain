"""Reverse engineering: documentation written from a Code Graph snapshot.

Pins that every section says how it was obtained and where it was read, that
traceability is verified references only, that the generated document becomes
a knowledge source only on request and retires the one before it, that it is
never requirement evidence for the code it describes, and that the AI part is
opt-in, receipted and quoted.
"""

import hashlib
import json
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from platform_core import code_docs
from platform_core.code_docs import process_next_docs_ai
from platform_core.code_knowledge import links
from platform_core.graphs import build_graph
from platform_core.models import (
    AIConfiguration,
    AIUsage,
    ApplicationFeature,
    CodeDocumentation,
    CodeFile,
    CodeRelationship,
    CodeRepository,
    CodeSnapshot,
    GraphRevision,
    KnowledgeEntry,
)
from platform_core.workbench import add_knowledge

from . import test_documents
from .test_code_factory import SETTINGS

MODELS = (
    "from django.db import models\n\n\n"
    "class Patient(models.Model):\n"
    "    site = models.ForeignKey(\"Site\", on_delete=models.CASCADE)\n"
    "    consent_withdrawn = models.BooleanField(default=False)\n\n"
    "    class Meta:\n"
    "        ordering = [\"id\"]\n"
)
URLS = 'urlpatterns = [path("api/patients/<int:pk>/export", export)]\n'
EXPORT = (
    "from care.models import Patient\n\n\n"
    "def export(request, pk):\n"
    "    # FR-09: exclude withdrawn consent\n"
    "    return Patient.objects.filter(consent_withdrawn=False)\n"
)
HELPER = (
    "# CARE-512 temporary workaround\nimport requests\nimport os\n"
    "from dataclasses import dataclass, field\n\n\n"
    "@dataclass\nclass Settings:\n"
    '    url: str = os.environ.get("URL", "")\n'
    "    tags: list[str] = field(default_factory=list)\n"
)


@override_settings(**SETTINGS)
class CodeDocsTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        patcher = patch("platform_core.secrets.application_secret", return_value="key")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls = 0
        self.requirements = add_knowledge(
            self.owner, self.app.pk, "Requirements",
            "| Requirement | Statement |\n| --- | --- |\n"
            "| FR-09 | Exports exclude patients who withdrew consent |\n"
            "| NFR-06 | Every export completes within five seconds |\n"
            "| CARE-401 | Export ignores withdrawn consent |",
        )
        self.publish([self.requirements])
        self.repository = CodeRepository.objects.create(
            application=self.app, added_by=self.owner, name="acme/care", external_id="acme/care",
            status="ready", source_url="https://github.com/acme/care",
        )
        self.snapshot = CodeSnapshot.objects.create(
            repository=self.repository, number=1, commit_sha="a" * 40, manifest_digest="m",
            languages=[{"name": "Python", "files": 4, "share": 100.0}],
            test_setup={"python": "pytest", "ci": True},
        )
        self.models = self.code("care/models.py", MODELS, role="leaf")
        self.urls = self.code(
            "care/urls.py", URLS, role="entry",
            routes=[{"method": "*", "path": "/api/patients/*/export",
                     "raw": "api/patients/<int:pk>/export"}],
        )
        self.export = self.code("care/export.py", EXPORT, role="service")
        self.helper = self.code("tools/helper.py", HELPER, role="orphan",
                                imports=[{"module": "requests", "names": [], "line": 2,
                                          "level": 0},
                                         {"module": "os", "names": [], "line": 3, "level": 0}])
        for source, target in ((self.urls, self.export), (self.export, self.models)):
            CodeRelationship.objects.create(
                snapshot=self.snapshot, source=source, target=target, kind="import",
                confidence="static", evidence={"lines": [1], "names": []},
            )

    def publish(self, entries, number=1):
        data, quality = build_graph(entries, number)
        return GraphRevision.objects.create(
            application=self.app, number=number, data=data, quality=quality,
            published_at=timezone.now(), fingerprint=f"test-{number}",
        )

    def code(self, path, content, **fields):
        return CodeFile.objects.create(
            snapshot=self.snapshot, path=path, content=content, language="python",
            digest=hashlib.sha256(content.encode()).hexdigest(),
            lines=len(content.splitlines()), **fields,
        )

    def generate(self):
        return code_docs.generate(self.owner, self.app.pk, self.repository.pk)

    def test_every_section_is_read_from_the_snapshot_and_says_where(self):
        doc = self.generate()
        s = doc.sections
        self.assertEqual(s["overview"]["entry_points"], ["care/urls.py"])
        self.assertEqual(s["overview"]["tests"], ["pytest"])
        model = next(m for m in s["data_model"]["models"] if m["name"] == "Patient")
        self.assertEqual((model["name"], model["kind"], model["path"], model["line"]),
                         ("Patient", "Django model", "care/models.py", 4))
        fields = {f["name"]: f for f in model["fields"]}
        self.assertEqual(set(fields), {"site", "consent_withdrawn"})
        self.assertEqual((fields["site"]["kind"], fields["site"]["related"]),
                         ("ForeignKey", "Site"))
        self.assertEqual(fields["consent_withdrawn"]["line"], 6)
        route = s["endpoints"]["rows"][0]
        self.assertEqual((route["path"], route["file"], route["line"]),
                         ("/api/patients/*/export", "care/urls.py", 1))
        # The standard library is not a third-party dependency.
        self.assertEqual(s["dependencies"]["external"], [{"module": "requests", "files": 1}])
        settings = next(m for m in s["data_model"]["models"] if m["name"] == "Settings")
        self.assertEqual([(f["name"], f["kind"]) for f in settings["fields"]],
                         [("url", "str"), ("tags", "list[str]")])
        self.assertEqual(sum(row["size"] for row in s["components"]["rows"]), 4)

    def test_traceability_is_verified_references_and_both_kinds_of_gap(self):
        trace = self.generate().sections["traceability"]
        self.assertTrue(trace["available"])
        self.assertEqual([row["requirement"] for row in trace["rows"]], ["FR-09"])
        place = trace["rows"][0]["code"][0]
        self.assertEqual((place["path"], place["line"]), ("care/export.py", 5))
        self.assertEqual([row["requirement"] for row in trace["no_code"]],
                         ["CARE-401", "NFR-06"])
        # A ticket the sources never mention is a gap; a record number is not.
        self.assertEqual([row["requirement"] for row in trace["no_source"]], ["CARE-512"])

    def test_the_document_labels_what_is_inferred(self):
        text = code_docs.markdown(self.generate())
        for heading in ("## Overview (read from code)", "## Components (inferred)",
                        "## Data model (read from code)", "## API endpoints (read from code)",
                        "## Traceability (verified references, not proof of implementation)"):
            self.assertIn(heading, text)
        self.assertIn("`care/models.py:6` - `consent_withdrawn = models.BooleanField", text)
        self.assertIn("not evidence that the code meets a requirement", text)

    def test_generating_calls_no_model(self):
        with patch("platform_core.ai.invoke_ai") as invoke:
            self.generate()
        invoke.assert_not_called()

    def test_added_to_knowledge_only_on_request_and_retires_the_last(self):
        doc = self.generate()
        self.assertFalse(KnowledgeEntry.objects.filter(source__startswith="code-graph://").exists())
        first = code_docs.add_to_knowledge(self.owner, doc)
        self.assertEqual(first.source, f"code-graph://{self.repository.pk}")
        self.assertIn(first.content, [code_docs.markdown(doc)])
        # The same text again adds nothing.
        self.assertEqual(code_docs.add_to_knowledge(self.owner, doc).pk, first.pk)
        newer = CodeSnapshot.objects.create(
            repository=self.repository, number=2, commit_sha="b" * 40, manifest_digest="n",
        )
        CodeFile.objects.create(snapshot=newer, path="care/new.py", content="X = 1\n",
                                digest=hashlib.sha256(b"X = 1\n").hexdigest(), language="python")
        second = code_docs.add_to_knowledge(self.owner, self.generate())
        first.refresh_from_db()
        self.assertFalse(first.active)
        self.assertTrue(second.active)
        self.assertEqual(
            KnowledgeEntry.objects.filter(source=first.source, active=True).count(), 1
        )

    def test_reverse_engineered_documentation_is_never_requirement_evidence(self):
        doc = self.generate()
        entry = code_docs.add_to_knowledge(self.owner, doc)
        revision = self.publish([self.requirements, entry], number=2)
        found, _ = links(revision, self.snapshot)
        self.assertTrue(found)
        self.assertNotIn(str(entry.pk), {row["entry_id"] for row in found})

    def test_the_page_works_without_script(self):
        page = reverse("code-docs", args=[self.app.pk])
        self.assertContains(self.client.get(page), "Generate documentation")
        response = self.client.post(page, {"action": "generate",
                                           "repository": str(self.repository.pk)})
        self.assertEqual(response.status_code, 302)
        doc = CodeDocumentation.objects.get()
        shown = self.client.get(f"{page}?repository={self.repository.pk}")
        self.assertContains(shown, "Patient")
        self.assertContains(shown, "/api/patients/*/export")
        self.assertContains(shown, "Add to knowledge sources")
        response = self.client.post(page, {"action": "add", "documentation": str(doc.pk),
                                           "repository": str(self.repository.pk)})
        self.assertEqual(response.status_code, 302)
        self.assertContains(self.client.get(f"{page}?repository={self.repository.pk}"),
                            "In knowledge sources")

    def test_access_is_denied_by_default(self):
        page = reverse("code-docs", args=[self.app.pk])
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(page).status_code, 404)
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(page).status_code, 200)
        response = self.client.post(page, {"action": "generate",
                                           "repository": str(self.repository.pk)})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(CodeDocumentation.objects.exists())
        self.client.force_login(self.owner)
        ApplicationFeature.objects.create(application=self.app, key="code_docs", enabled=False)
        self.assertEqual(self.client.get(page).status_code, 403)

    def test_a_repository_of_another_application_is_not_found(self):
        from django.http import Http404

        other = CodeRepository.objects.create(
            application=self.other, added_by=self.owner, name="x/y", external_id="x/y",
            status="ready", source_url="https://github.com/x/y",
        )
        with self.assertRaises(Http404):
            code_docs.generate(self.owner, self.app.pk, other.pk)

    # ---------------------------------------------------------------- AI part

    def configure(self):
        AIConfiguration.objects.update_or_create(
            application=self.app, purpose="code_docs",
            defaults={"provider": "claude", "model": "claude-sonnet-5",
                      "input_rate": Decimal("2"), "output_rate": Decimal("10"),
                      "configured_by": self.owner, "enabled": True},
        )

    def describe(self, doc, *answers):
        replies = list(answers)

        def reply(config, question, citations, token, **options):
            self.calls += 1
            self.seen = citations
            return ({"id": f"req-{self.calls}",
                     "usage": {"prompt_tokens": 800, "completion_tokens": 60}}, replies.pop(0))

        with patch("platform_core.claude_agents.completion", side_effect=reply):
            code_docs.request_ai(self.owner, doc)
            process_next_docs_ai()
        doc.refresh_from_db()
        return doc

    def test_ai_paragraphs_are_kept_only_with_quotes_from_their_component(self):
        self.configure()
        doc = self.generate()
        component = next(
            row for row in doc.sections["components"]["rows"]
            if str(self.export.pk) in row["ids"]
        )
        answer = json.dumps({"components": [{"name": component["name"], "paragraphs": [
            {"text": "Exports patients who have not withdrawn consent.",
             "quotes": [{"source": f"code:{self.export.pk}",
                         "text": "Patient.objects.filter(consent_withdrawn=False)"}]},
            {"text": "Sends email.",
             "quotes": [{"source": f"code:{self.export.pk}", "text": "send_mail(recipient)"}]},
        ]}]})
        doc = self.describe(doc, answer)
        self.assertEqual(doc.ai_status, "ready", doc.ai_error)
        paragraphs = doc.ai_result["components"][0]["paragraphs"]
        self.assertEqual([p["text"] for p in paragraphs],
                         ["Exports patients who have not withdrawn consent."])
        self.assertEqual(paragraphs[0]["quotes"][0]["line"], 6)
        self.assertEqual(doc.ai_result["dropped"], 1)
        self.assertEqual(AIUsage.objects.get().purpose, "code_docs")
        text = code_docs.markdown(doc)
        self.assertIn("## Components described by AI", text)
        self.assertIn("AI-written.", text)
        # The same material again is not paid for.
        self.describe(doc)
        self.assertEqual(self.calls, 1)

    def test_the_ai_part_needs_the_purpose_configured(self):
        from django.core.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            code_docs.request_ai(self.owner, self.generate())
        self.assertFalse(AIUsage.objects.exists())
