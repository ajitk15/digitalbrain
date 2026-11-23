"""The document review pack (`document_review_pack`): planted contradictions, reviewed
with tools and with one call.

The labels are held to the CarePath files they describe, so a planted line cannot
be one the documents do not hold. A run uses a patched provider for the one call
and a scripted model for the agent, so the review, its judge and the receipts are
the ones the button runs.
"""

import json
import tempfile
import uuid
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings

from platform_core import document_review_pack
from platform_core.agent_runtime import harness
from platform_core.agent_runtime.scripted import Script
from platform_core.models import AgentMeasurement, AIConfiguration, AIUsage, KnowledgeEntry
from platform_core.workbench import add_knowledge

from . import test_documents

DOCS = document_review_pack.DOCS
NFR = "| NFR-09 | Audit records are immutable and retained for six years. |"


class LabelTests(SimpleTestCase):
    def test_every_planted_line_is_one_line_of_the_file_it_names(self):
        names = {path.stem for path in DOCS.rglob("*") if path.is_file()}
        for key, item in document_review_pack.load_pack()["items"].items():
            with self.subTest(item=key):
                text = (DOCS / item["path"]).read_text(encoding="utf-8")
                self.assertEqual(text.count(item["find"]), 1)
                self.assertEqual(Path(item["path"]).name, item["document"])
                self.assertIn(item["planted"], item["replace"])
                self.assertNotIn(item["planted"], item["find"])
                self.assertNotIn(item["planted"], text)
                for other in item["others"]:
                    # A source the planted value contradicts is a CarePath document.
                    self.assertTrue(any(other in name for name in names), other)

    def test_a_hit_quotes_the_planted_text_and_a_link_names_a_listed_source(self):
        item = {"planted": "Two years minimum", "others": ["non-functional-requirements"]}
        mine = {"aspect": "contradiction", "quote": "| Audit events | Two years minimum,"}
        self.assertEqual(document_review_pack.score([mine], item),
                         {"found": True, "linked": False})
        linked = {**mine, "other": {"title": "non-functional-requirements.xlsx"}}
        self.assertEqual(document_review_pack.score([linked], item),
                         {"found": True, "linked": True})
        clarity = {**linked, "aspect": "clarity"}
        elsewhere = {**linked, "quote": "Consent records | Life of the record"}
        self.assertEqual(document_review_pack.score([clarity, elsewhere], item),
                         {"found": False, "linked": False})


class RunTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        AIConfiguration.objects.create(
            application=self.app, purpose="document_review", provider="claude",
            model="claude-sonnet-5", input_rate=Decimal("3"), output_rate=Decimal("15"),
            configured_by=self.owner,
        )
        credential = patch("platform_core.ai.provider_credential", return_value="unused")
        credential.start()
        self.addCleanup(credential.stop)
        text = (DOCS / "02-design/data-model.md").read_text(encoding="utf-8")
        self.model = add_knowledge(self.owner, self.app.pk, "data-model.md", text)
        self.nfr = add_knowledge(self.owner, self.app.pk, "non-functional-requirements.xlsx", NFR)
        # Two more of the pack's documents, so most items are asked; the runbook is
        # left out, so its two items show what a missing document says.
        for path in ("03-build/coding-standards.md", "08-quality-samples/escalation-policy.md"):
            add_knowledge(self.owner, self.app.pk, Path(path).name,
                          (DOCS / path).read_text(encoding="utf-8"))

    def finding(self, other=None):
        return {
            "aspect": "contradiction",
            "quote": "| Audit events | Two years minimum, never deleted by application code |",
            "finding": "Retention is shorter than required.",
            "suggestion": "Six years.",
            **({"other": other} if other else {}),
        }

    def measured(self):
        alone = json.dumps({"findings": [self.finding()]})
        linked = json.dumps({"findings": [self.finding(
            {"source": str(self.nfr.pk), "quote": "retained for six years"}
        )]})

        class Model:
            def run(self, *args, tools=None, **kwargs):
                script = Script([("search_knowledge", {"query": "audit records retained"})],
                                linked)
                return script.run(*args, tools=tools, **kwargs)

        def one_call(config, question, citations, token, **kwargs):
            # Each call its own request id, as a provider's are: one id would make
            # every later receipt the first one again.
            return ({"id": f"one-{uuid.uuid4()}",
                     "usage": {"prompt_tokens": 100, "completion_tokens": 20}}, alone)

        with (
            override_settings(DOCUMENT_REVIEW_AGENT_TOOLS=True),
            patch("platform_core.claude_agents.completion", side_effect=one_call),
            patch.object(harness, "runtime_for", return_value=Model()),
        ):
            return document_review_pack.run(self.owner, self.app)

    def test_one_call_finds_the_planted_line_and_only_the_agent_links_it(self):
        before = KnowledgeEntry.objects.get(pk=self.model.pk).content
        record = self.measured()
        rows = {(row["item"], row["mode"]): row for row in record["details"]}
        self.assertEqual(
            (rows[("audit-retention-shortened", "one_shot")]["found"],
             rows[("audit-retention-shortened", "one_shot")]["linked"]),
            (True, False),
        )
        self.assertEqual(
            (rows[("audit-retention-shortened", "tools")]["found"],
             rows[("audit-retention-shortened", "tools")]["linked"]),
            (True, True),
        )
        # Items whose document is not here are said, and cost nothing.
        self.assertEqual(rows[("sev1-paged-late", "tools")]["failure"],
                         "No source here is operations-runbook.txt.")
        alone, tools = record["metrics"]["one_shot"], record["metrics"]["tools"]
        self.assertEqual((alone["found"], alone["linked"], tools["found"], tools["linked"]),
                         (1, 0, 1, 1))
        # Three items asked: one call each, and two scripted turns each with tools.
        self.assertEqual(AIUsage.objects.filter(purpose="document_review").count(), 9)
        # The planted line was reviewed in memory; the stored source is unchanged.
        self.assertEqual(KnowledgeEntry.objects.get(pk=self.model.pk).content, before)
        self.assertTrue(document_review_pack.verdict(record["metrics"]).startswith(
            "Tools clear the bar"
        ))

    def test_the_command_records_it_for_the_document_review_agents_page(self):
        record = self.measured()
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(document_review_pack, "RECORDS", Path(folder)):
                with patch.object(document_review_pack, "run", return_value=record):
                    out = StringIO()
                    call_command("document_review_pack", str(self.app.pk), "--as", "owner",
                                 "--record", stdout=out)
        self.assertIn("Kept for its agent's page", out.getvalue())
        row = AgentMeasurement.objects.get()
        self.assertEqual((row.agent, row.pack, row.verdict),
                         ("document-review", "document_review_pack", "clears"))

    def test_it_needs_document_review_configured(self):
        from django.core.exceptions import ValidationError

        AIConfiguration.objects.filter(purpose="document_review").delete()
        with self.assertRaises(ValidationError):
            document_review_pack.run(self.owner, self.app)


class NothingComparedTests(TestCase):
    """A pack that could ask none of its cases here says so and records nothing:
    "no better" from a run that compared nothing would be a verdict on the agent's
    page that no comparison made."""

    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        AIConfiguration.objects.create(
            application=self.app, purpose="document_review", provider="claude",
            model="claude-sonnet-5", input_rate=Decimal("3"), output_rate=Decimal("15"),
            configured_by=self.owner,
        )

    def test_no_document_here_is_nothing_compared_and_nothing_recorded(self):
        record = document_review_pack.run(self.owner, self.app)
        self.assertEqual(record["metrics"]["tools"]["failed"],
                         record["metrics"]["tools"]["items"])
        self.assertTrue(document_review_pack.verdict(record["metrics"]).startswith(
            "Nothing was compared"
        ))
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(document_review_pack, "RECORDS", Path(folder)):
                out = StringIO()
                call_command("document_review_pack", str(self.app.pk), "--as", "owner",
                             "--record", stdout=out)
                self.assertEqual(list(Path(folder).iterdir()), [])
        self.assertIn("Not recorded: nothing was compared.", out.getvalue())
        self.assertFalse(AgentMeasurement.objects.exists())
        self.assertFalse(AIUsage.objects.exists())


class NothingComparedRuleTests(SimpleTestCase):
    def test_either_way_with_every_case_failed_or_none_is_nothing_compared(self):
        from platform_core.agent_measurements import nothing_compared

        asked = {"items": 5, "failed": 1}
        self.assertFalse(nothing_compared({"one_shot": asked, "tools": asked}))
        self.assertTrue(nothing_compared({"one_shot": asked,
                                          "tools": {"items": 5, "failed": 5}}))
        self.assertTrue(nothing_compared({"one_shot": {"cases": 0}, "tools": {"cases": 0}}))
        # A pack of one case says only whether it failed.
        self.assertTrue(nothing_compared({"one_shot": {"asked": 6, "failed": 1}}))
        self.assertFalse(nothing_compared({"one_shot": {"asked": 6, "failed": 0}}))
        # A rate limit that failed most questions both ways compares nothing; one way
        # failing most of them is a result, and counts against that way.
        limited = {"questions": 16, "failed": 15}
        self.assertTrue(nothing_compared({"one_shot": limited, "tools": limited}))
        self.assertFalse(nothing_compared({"one_shot": {"questions": 16, "failed": 0},
                                           "tools": limited}))
        # Nothing kept to score either way is no comparison either.
        empty = {"asked": 6, "components": 0, "failed": 0}
        self.assertTrue(nothing_compared({"one_shot": empty, "tools": empty}))
        self.assertFalse(nothing_compared({"one_shot": empty,
                                           "tools": {**empty, "components": 4}}))
