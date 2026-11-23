"""Code documentation's pack (`docs_pack`): with tools and with one call.

The one call is answered by a patched provider and the agent by a scripted model,
so each way runs exactly what the button runs - `code_docs.describe` and its
`judge` - and the scores, the receipts and the record are read from what happened.
"""

import json
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase, override_settings

from platform_core import code_docs, docs_pack
from platform_core.agent_runtime import harness
from platform_core.agent_runtime.scripted import Script
from platform_core.models import AgentMeasurement, AgentStep, AIUsage, CodeDocumentation

from . import test_code_docs
from .test_code_factory import SETTINGS

QUOTE = "Patient.objects.filter(consent_withdrawn=False)"


@override_settings(**SETTINGS)
@patch.object(code_docs, "AI_LINES_PER_FILE", 2)
class DocsPackTests(TestCase):
    setUp = test_code_docs.CodeDocsTests.setUp
    publish = test_code_docs.CodeDocsTests.publish
    code = test_code_docs.CodeDocsTests.code
    generate = test_code_docs.CodeDocsTests.generate
    configure = test_code_docs.CodeDocsTests.configure

    def answer(self, doc):
        name = next(
            row["name"] for row in doc.sections["components"]["rows"]
            if str(self.export.pk) in row["ids"]
        )
        return json.dumps({"components": [{"name": name, "paragraphs": [
            {"text": "Exports consenting patients.",
             "quotes": [{"source": f"code:{self.export.pk}", "text": QUOTE}]},
        ]}]})

    def measured(self, doc, **options):
        text = self.answer(doc)

        def one_call(config, question, citations, token, **kwargs):
            return ({"id": "one", "usage": {"prompt_tokens": 100, "completion_tokens": 20}},
                    text)

        script = Script([("read_file", {"path": "care/export.py"})], text)
        with (
            patch("platform_core.claude_agents.completion", side_effect=one_call),
            patch.object(harness, "runtime_for", return_value=script),
        ):
            return docs_pack.run(self.owner, self.app, **options)

    def test_reading_the_file_keeps_the_paragraph_one_call_could_not_quote(self):
        self.configure()
        doc = self.generate()
        record = self.measured(doc)
        alone, tools = record["metrics"]["one_shot"], record["metrics"]["tools"]
        # One call saw two lines of the file, so its quote was not in front of it.
        self.assertEqual((alone["components"], alone["paragraphs"], alone["dropped"]), (0, 0, 1))
        # The file says why, so a run that kept nothing can be read.
        first = next(row for row in record["details"] if row["mode"] == "one_shot")
        self.assertEqual(first["dropped_reasons"], {"quote not found": 1})
        self.assertEqual((tools["components"], tools["paragraphs"], tools["files_quoted"]),
                         (1, 1, 1))
        self.assertEqual((alone["kept_share"], tools["kept_share"]), (0.0, 100.0))
        self.assertEqual((alone["turns"], tools["turns"], tools["tool_calls"]), (1, 2, 1))
        self.assertEqual(AIUsage.objects.filter(purpose="code_docs").count(), 3)
        self.assertTrue(docs_pack.verdict(record["metrics"]).startswith("Tools clear the bar"))
        self.assertEqual(record["snapshot"], {"repository": "acme/care", "number": 1})
        self.assertEqual(record["pack"], docs_pack.pack_digest(doc))
        # Nothing is written on the documentation: no result, no step.
        doc.refresh_from_db()
        self.assertEqual((doc.ai_status, doc.ai_result), ("", {}))
        self.assertFalse(AgentStep.objects.exists())

    def test_the_command_records_it_for_the_documentation_agents_page(self):
        self.configure()
        doc = self.generate()
        with tempfile.TemporaryDirectory() as folder:
            records = Path(folder) / "docs"
            text = self.answer(doc)
            with (
                patch.object(docs_pack, "RECORDS", records),
                patch("platform_core.claude_agents.completion", return_value=(
                    {"id": "one", "usage": {"prompt_tokens": 100, "completion_tokens": 20}}, text,
                )),
                patch.object(harness, "runtime_for", return_value=Script(
                    [("read_file", {"path": "care/export.py"})], text
                )),
            ):
                out = StringIO()
                call_command("docs_pack", str(self.app.pk), "--as", "owner", "--record",
                             stdout=out)
            self.assertTrue(list(records.glob("*.json")))
        self.assertIn("Kept for its agent's page", out.getvalue())
        self.assertIn("one_shot: described 0/", out.getvalue())
        self.assertIn("dropped: 1 quote not found", out.getvalue())
        row = AgentMeasurement.objects.get()
        self.assertEqual((row.agent, row.pack, row.verdict), ("code-docs", "docs_pack", "clears"))

    def test_it_needs_the_task_configured_and_documentation_written(self):
        with self.assertRaises(ValidationError):
            docs_pack.run(self.owner, self.app)
        self.configure()
        with self.assertRaises(ValidationError):
            docs_pack.run(self.owner, self.app)
        self.assertFalse(AIUsage.objects.exists())

    def test_a_viewer_may_not_run_it(self):
        from django.core.exceptions import PermissionDenied
        from django.http import Http404

        self.configure()
        self.generate()
        with self.assertRaises((PermissionDenied, Http404)):
            docs_pack.run(self.viewer, self.app)

    def test_the_newest_documentation_of_the_named_repository_is_measured(self):
        self.configure()
        first = self.generate()
        second = self.generate()
        self.assertEqual(docs_pack.documentation(self.app, "acme/care").pk, second.pk)
        self.assertNotEqual(first.pk, second.pk)
        with self.assertRaises(ValidationError):
            docs_pack.documentation(self.app, "nobody/else")
        self.assertEqual(CodeDocumentation.objects.count(), 2)
