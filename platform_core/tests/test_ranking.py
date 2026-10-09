"""BM25 ranking, coverage first, and one citation per source before repeats."""

import hashlib
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings

from platform_core import serviceops
from platform_core.graph_ai import citations_from
from platform_core.graphs import build_graph
from platform_core.models import KnowledgeEntry
from platform_core.ranking import BM25, query_terms, spread, stem, terms
from platform_core.workbench import retrieve

from . import test_documents


class RankingTests(SimpleTestCase):
    def test_a_rare_word_outranks_a_common_one(self):
        texts = ["patient export record"] * 8 + ["patient consent record"]
        index = BM25([terms(text) for text in texts])
        best = index.rank(query_terms("patient consent"))[0]
        self.assertEqual(best[1], 8)

    def test_holding_more_of_the_question_comes_first(self):
        """One rare word must not carry a text past one holding the whole question."""
        texts = ["consent", "consent export check", "unrelated words only"]
        index = BM25([terms(text) for text in texts])
        ranked = index.rank_covering(query_terms("Is consent checked on export?"))
        self.assertEqual([position for _, position in ranked], [1, 0])

    def test_question_words_and_filler_are_not_terms(self):
        self.assertEqual(query_terms("What is the consent for, and why?"), ["consent"])
        self.assertEqual(terms("Exports refused"), ["export", "refus"])

    def test_spread_takes_each_source_once_before_any_twice(self):
        items = [("a", 1), ("a", 2), ("a", 3), ("b", 4), ("c", 5)]
        self.assertEqual(
            spread(items, key=lambda item: item[0], limit=4),
            [("a", 1), ("b", 4), ("c", 5), ("a", 2)],
        )

    def test_spread_stops_reading_once_it_has_enough_sources(self):
        def endless():
            number = 0
            while True:
                number += 1
                yield number

        self.assertEqual(spread(endless(), key=lambda item: item, limit=3), [1, 2, 3])

    def test_an_identifier_is_its_words(self):
        """settings.max_page_size answers a question about page size."""
        self.assertEqual(query_terms("settings.max_page_size"), ["setting", "max", "page", "size"])

    def test_requirement_and_require_are_one_term(self):
        self.assertEqual(terms("requirements require"), ["require", "require"])
        self.assertEqual(terms("deployment"), ["deploy"])
        # Short words keep their ending: "comment" is not "com".
        self.assertEqual(terms("comment documents"), ["comment", "document"])

    def test_against_is_filler(self):
        self.assertEqual(
            query_terms("protected against credential stuffing"),
            [
                "protect",
                "credential",
                "stuff",
            ],
        )

    def test_the_stemmer_itself_did_not_change(self):
        """Incident fingerprints use `stem`; the -ment fold lives in `term` only."""
        self.assertEqual(stem("requirement"), "requirement")
        self.assertEqual(stem("disk_full"), "disk_full")

    def test_serviceops_keeps_the_same_stemmer(self):
        """Moving the stemmer must not change a single incident fingerprint."""
        self.assertIs(serviceops.stem, stem)


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class RetrievalSpreadTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)

    def entry(self, title, content):
        return KnowledgeEntry.objects.create(
            application=self.app,
            author=self.owner,
            title=title,
            content=content,
            digest=hashlib.sha256(content.encode()).hexdigest(),
        )

    def test_one_document_cannot_take_every_graph_citation(self):
        loud = self.entry(
            "Release notes",
            "\n".join(f"Consent export note {number}." for number in range(20)),
        )
        quiet = self.entry("CARE-401", "The consent export is not checked.")
        entries = [loud, quiet]
        data, _ = build_graph(entries, len(entries))
        cited = citations_from(self.app.pk, data, 1, "consent export")
        self.assertEqual(len(cited), 8)
        self.assertIn(str(quiet.pk), {citation["id"] for citation in cited[:2]})

    def test_an_edited_source_is_ranked_by_what_it_says_now(self):
        entry = self.entry("Runbook", "Restart the queue worker.")
        self.assertEqual(retrieve(self.app, "queue worker")[0][1].pk, entry.pk)
        content = "Rotate the signing certificate."
        KnowledgeEntry.objects.filter(pk=entry.pk).update(
            content=content, digest=hashlib.sha256(content.encode()).hexdigest()
        )
        self.assertEqual(retrieve(self.app, "queue worker"), [])
        self.assertEqual(retrieve(self.app, "signing certificate")[0][1].pk, entry.pk)

    def test_a_source_is_split_once_not_on_every_question(self):
        from platform_core import ranking, workbench

        self.entry("Runbook", "Restart the queue worker when the queue backs up.")
        retrieve(self.app, "queue worker")
        with patch.object(ranking, "terms", wraps=ranking.terms) as split:
            retrieve(self.app, "queue backs up")
        # The question is split; the source, already split, is not.
        self.assertEqual(split.call_count, 1)
        self.assertTrue(workbench._passage_terms)

    def test_keyword_passages_spread_across_documents_and_match_stems(self):
        long = self.entry("Handbook", ("Consent checks happen at export. " * 60).strip())
        other = self.entry("CARE-401", "Consent is not checked before export.")
        found = retrieve(self.app, "Is consent checked before export?")
        documents = [entry.pk for _, entry, _ in found]
        self.assertEqual(set(documents[:2]), {long.pk, other.pk})
        self.assertLessEqual(len(found), 5)
