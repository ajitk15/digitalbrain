"""Code Factory's evidence follows the references its ticket names, after the direct matches."""

from unittest.mock import patch

from django.test import TestCase, override_settings

from platform_core import code_factory
from platform_core.graph_ai import MAX_REFERENCED, citations_from
from platform_core.graphs import build_graph, sources_for
from platform_core.models import KnowledgeEntry
from platform_core.workbench import add_knowledge

from . import test_documents

QUESTION = "CARE-501 Partner export ignores consent. The export never checks consent; see C-09."


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class ReferenceTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        add = lambda title, text: add_knowledge(self.owner, self.app.pk, title, text)  # noqa: E731
        self.ticket = add("CARE-501 Partner export ignores consent", QUESTION)
        self.words = add("Export guide", "Partner export checks consent before export.")
        # Shares no word with the question beyond the reference it names.
        self.matrix = add("Control matrix", "Finding F-01: C-09 is not implemented.")
        self.unrelated = add("Rota", "Who is on call this week.")

    def graph(self):
        entries = list(sources_for(self.app.pk))
        data, _ = build_graph(entries, len(entries))
        return data

    def ask(self, data, **kwargs):
        return citations_from(self.app.pk, data, 1, QUESTION, viewer=self.owner, **kwargs)

    def test_chat_is_unchanged_and_references_come_after(self):
        data = self.graph()
        direct = self.ask(data, limit=1)
        followed = self.ask(data, limit=1, references=MAX_REFERENCED)
        self.assertEqual(self.ask(data, limit=1, references=0), direct)
        # Nothing the direct matches cited moves or goes.
        self.assertEqual(followed[: len(direct)], direct)
        added = followed[len(direct) :]
        self.assertIn(str(self.matrix.pk), [c["id"] for c in added])
        found = next(c for c in added if c["id"] == str(self.matrix.pk))
        self.assertIn("Reached by: names C-09", found["excerpt"])
        self.assertIn(found["quote"], self.matrix.content)
        self.assertNotIn(str(self.unrelated.pk), [c["id"] for c in followed])

    def test_a_question_naming_no_reference_follows_none(self):
        data = self.graph()
        question = "Does the partner export check consent?"
        plain = citations_from(self.app.pk, data, 1, question, viewer=self.owner, limit=1)
        followed = citations_from(
            self.app.pk, data, 1, question, viewer=self.owner, limit=1, references=4
        )
        self.assertEqual(followed, plain)

    def test_a_changed_source_is_not_followed(self):
        data = self.graph()
        KnowledgeEntry.objects.filter(pk=self.matrix.pk).update(digest="changed")
        followed = self.ask(data, limit=1, references=MAX_REFERENCED)
        self.assertNotIn(str(self.matrix.pk), [c["id"] for c in followed])

    def test_a_source_naming_its_own_key_is_not_a_reference(self):
        """The ticket names CARE-501 throughout; that is not a cross-reference to it."""
        data = self.graph()
        followed = self.ask(data, limit=1, references=MAX_REFERENCED)
        added = followed[1:]
        self.assertTrue(added)
        for citation in added:
            self.assertNotIn("names CARE-501", citation["excerpt"])

    def test_code_factory_asks_for_references(self):
        with patch("platform_core.graph_ai.graph_citations", return_value=[]) as asked:
            code_factory.evidence_for(self.app.pk, QUESTION, "1", self.owner)
        self.assertEqual(asked.call_args.kwargs["references"], MAX_REFERENCED)
