"""Keyword search chooses its candidates by full text on PostgreSQL, by substring elsewhere."""

import unittest

from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings

from platform_core.workbench import (
    CANDIDATE_SOURCES,
    add_knowledge,
    candidate_sources,
    full_text_query,
    retrieve,
)

from . import test_documents


class QueryTests(SimpleTestCase):
    def test_each_word_is_a_prefix_and_any_may_match(self):
        self.assertEqual(
            full_text_query("Which requirements protect login?"),
            "require:* | protect:* | login:*",
        )

    def test_nothing_in_a_question_becomes_tsquery_syntax(self):
        query = full_text_query("a & b | !c :* (d) 'e' <-> max_page_size")
        self.assertNotIn("&", query)
        self.assertNotIn("!", query)
        self.assertNotIn("(", query)
        self.assertNotIn("<->", query)
        self.assertEqual(query, "max:* | page:* | size:*")

    def test_a_question_of_filler_has_no_query(self):
        self.assertEqual(full_text_query("What is it?"), "")


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class CandidateTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)

    def test_only_this_applications_answerable_sources_are_candidates(self):
        mine = add_knowledge(self.owner, self.app.pk, "Runbook", "Restart the queue worker.")
        hidden = add_knowledge(self.owner, self.app.pk, "Roster", "Queue worker rota.")
        hidden.sensitivity = "restricted"
        hidden.save()
        found = [entry.pk for entry in candidate_sources(self.app, "queue worker")]
        self.assertEqual(found, [mine.pk])
        self.assertEqual(candidate_sources(self.other, "queue worker"), [])

    @unittest.skipUnless(
        connection.vendor == "postgresql",
        "PostgreSQL full-text search runs only against PostgreSQL; this run uses "
        f"{connection.vendor}. Run the suite with a PostgreSQL DATABASE to exercise it.",
    )
    def test_postgres_ranks_by_full_text_and_keeps_the_best_hundred(self):
        for number in range(CANDIDATE_SOURCES + 5):
            add_knowledge(self.owner, self.app.pk, f"Note {number}", "An unrelated queue note.")
        best = add_knowledge(
            self.owner, self.app.pk, "Worker runbook", "Restart the queue worker; queue worker."
        )
        found = candidate_sources(self.app, "queue worker restart")
        self.assertEqual(len(found), CANDIDATE_SOURCES)
        self.assertEqual(found[0].pk, best.pk)
        self.assertEqual(retrieve(self.app, "queue worker restart")[0][1].pk, best.pk)

    @unittest.skipUnless(
        connection.vendor == "postgresql",
        "The stored search vectors exist only on PostgreSQL; this run uses "
        f"{connection.vendor}.",
    )
    def test_postgres_reads_a_stored_vector_that_follows_the_text(self):
        from platform_core import chunks
        from platform_core.models import KnowledgeEntry

        entry = add_knowledge(self.owner, self.app.pk, "Runbook", "Restart the queue worker.")
        self.assertEqual([e.pk for e in candidate_sources(self.app, "failover")], [])
        # Rewritten in place, as masking does: the column is regenerated with the
        # row, so nothing has to remember to refresh it.
        KnowledgeEntry.objects.filter(pk=entry.pk).update(
            content="Database failover drill.", digest="1" * 64
        )
        self.assertEqual([e.pk for e in candidate_sources(self.app, "failover")], [entry.pk])
        self.assertEqual(
            [c.entry_id for c in chunks.candidates(self.app, "failover")], [entry.pk]
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT tablename, indexdef FROM pg_indexes WHERE indexname IN "
                "('platform_core_knowledge_search', 'platform_core_chunk_search')"
            )
            indexes = dict(cursor.fetchall())
        self.assertEqual(
            set(indexes), {"platform_core_knowledgeentry", "platform_core_knowledgechunk"}
        )
        self.assertTrue(all("(search_vector)" in d for d in indexes.values()), indexes)
