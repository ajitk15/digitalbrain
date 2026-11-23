"""`measure_agents`: which packs are ready and what the rest need, spending nothing,
then every ready pack run with --record."""

from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from platform_core import measure_agents
from platform_core.models import AIConfiguration, AIUsage, BenchmarkQuestion
from platform_core.workbench import add_knowledge

from . import test_documents


class MeasureAgentsTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)

    def configure(self, *purposes):
        for purpose in purposes:
            AIConfiguration.objects.create(
                application=self.app, purpose=purpose, provider="claude",
                model="claude-sonnet-5", input_rate=Decimal("3"), output_rate=Decimal("15"),
                configured_by=self.owner,
            )

    def said(self):
        return {check.pack: (ok, text) for check, ok, text in measure_agents.readiness(self.app)}

    def test_an_unconfigured_task_is_said_first(self):
        said = self.said()
        self.assertEqual(said["chat_pack"], (False, "Set up Chat conversation in AI settings."))
        self.assertFalse(any(ok for ok, _ in said.values()))

    def test_each_pack_says_what_it_is_missing(self):
        self.configure("chat", "plan_drafting", "code_review", "code_docs", "document_review",
                       "serviceops_triage")
        said = self.said()
        self.assertIn("--load-questions", said["chat_pack"][1])
        self.assertIn("documentation/demo/docs", said["document_review_pack"][1])
        self.assertIn("Nothing is published", said["analysis_pack"][1])
        self.assertIn("Code Repos", said["factory_pack"][1])
        self.assertIn("Documentation tab", said["docs_pack"][1])
        self.assertIn("confirmed", said["triage_pack"][1])

    def test_questions_are_loaded_once_and_make_chat_ready(self):
        self.configure("chat")
        self.assertEqual(measure_agents.load_questions(self.app, self.owner), 16)
        self.assertEqual(measure_agents.load_questions(self.app, self.owner), 0)
        self.assertEqual(BenchmarkQuestion.objects.filter(application=self.app).count(), 16)
        self.assertEqual(self.said()["chat_pack"], (True, "16 benchmark questions"))

    def test_a_document_the_pack_plants_in_makes_document_review_ready(self):
        self.configure("document_review")
        text = (measure_agents.ROOT / "documentation/demo/docs/02-design/data-model.md"
                ).read_text(encoding="utf-8")
        add_knowledge(self.owner, self.app.pk, "docs/02-design/data-model.md", text)
        self.assertEqual(self.said()["document_review_pack"],
                         (True, "1 of its 5 documents are here"))

    def test_without_run_nothing_is_spent_and_with_run_only_ready_packs_run(self):
        self.configure("chat")
        out = StringIO()
        call_command("measure_agents", str(self.app.pk), "--as", "owner", "--load-questions",
                     stdout=out)
        self.assertIn("ready   Chat agent (chat_pack): 16 benchmark questions", out.getvalue())
        self.assertIn("Nothing was spent", out.getvalue())
        self.assertFalse(AIUsage.objects.exists())
        ran = []
        with patch("platform_core.management.commands.measure_agents.call_command",
                   side_effect=lambda *args, **kwargs: ran.append(args)):
            call_command("measure_agents", str(self.app.pk), "--as", "owner", "--run",
                         stdout=StringIO())
        self.assertEqual(ran, [("chat_pack", str(self.app.pk), "--as", "owner", "--record")])

    def test_someone_who_cannot_open_the_application_is_refused(self):
        from django.core.management import CommandError

        from platform_core.models import User

        User.objects.create_user("stranger")
        with self.assertRaises(CommandError):
            call_command("measure_agents", str(self.app.pk), "--as", "stranger",
                         stdout=StringIO())
