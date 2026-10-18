"""A source's passages are rows, kept in step with it, and can answer keyword search.

Phase 2 of the scale work: `KnowledgeChunk` holds the windows `retrieve` used to
cut per question, written in the transaction that writes the source, retired
with it, cut again when its text or title changes, and backfilled for sources
stored before. `retrieve(candidates="chunks")` ranks them exactly as it ranks
passages cut from whole sources.
"""

import io
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings

from digitalbrain.configuration import load_config
from platform_core import chunks
from platform_core.models import KnowledgeChunk, KnowledgeEntry
from platform_core.workbench import PASSAGE_LENGTH, PASSAGE_STEP, add_knowledge, retrieve

from . import test_documents

LONG = "".join(f"Line {n} about queue depth and node routing.\n" for n in range(120))


@override_settings(
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    DOCUMENT_AUTO_CONVERT=False,
)
class ChunkTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)

    def source(self, title="Routing", content=LONG):
        return add_knowledge(self.owner, self.app.pk, title, content)

    def passages(self, entry):
        return list(
            KnowledgeChunk.objects.filter(entry=entry)
            .order_by("ordinal")
            .values_list("offset", "text", "heading")
        )

    def test_a_new_source_is_written_as_the_windows_search_cut(self):
        entry = self.source()
        expected = [
            (offset, LONG[offset : offset + PASSAGE_LENGTH], "Routing")
            for offset in range(0, len(LONG), PASSAGE_STEP)
        ]
        self.assertGreater(len(expected), 2)
        self.assertEqual(self.passages(entry), expected)
        entry.refresh_from_db()
        self.assertEqual(entry.chunked_digest, entry.digest)
        self.assertTrue(all(c.application_id == self.app.pk for c in KnowledgeChunk.objects.all()))

    def test_a_retired_source_keeps_its_text_but_not_a_second_copy(self):
        entry = self.source()
        KnowledgeEntry.objects.filter(pk=entry.pk).update(active=False)
        self.assertFalse(KnowledgeChunk.objects.filter(entry=entry).exists())
        entry.refresh_from_db()
        self.assertEqual(entry.chunked_digest, "")
        KnowledgeEntry.objects.filter(pk=entry.pk).update(active=True)
        self.assertTrue(KnowledgeChunk.objects.filter(entry=entry).exists())

    def test_a_saved_retirement_clears_them_too(self):
        entry = self.source()
        entry.active = False
        entry.save(update_fields=["active"])
        self.assertFalse(KnowledgeChunk.objects.filter(entry=entry).exists())

    def test_text_rewritten_in_place_is_cut_again(self):
        entry = self.source()
        KnowledgeEntry.objects.filter(pk=entry.pk).update(content="Short now.", digest="1" * 64)
        self.assertEqual(self.passages(entry), [(0, "Short now.", "Routing")])
        entry.refresh_from_db()
        self.assertEqual(entry.chunked_digest, "1" * 64)

    def test_a_new_title_is_ranked_with_every_passage(self):
        entry = self.source()
        entry.title = "Queues"
        entry.save(update_fields=["title"])
        self.assertEqual({heading for _, _, heading in self.passages(entry)}, {"Queues"})

    def test_an_unrelated_write_does_not_rewrite_them(self):
        entry = self.source()
        first = set(KnowledgeChunk.objects.values_list("pk", flat=True))
        KnowledgeEntry.objects.filter(pk=entry.pk).update(quality={"x": 1})
        entry.save()
        self.assertEqual(set(KnowledgeChunk.objects.values_list("pk", flat=True)), first)

    def test_deleting_a_source_deletes_its_passages(self):
        entry = self.source()
        KnowledgeEntry.objects.filter(pk=entry.pk).delete()
        self.assertFalse(KnowledgeChunk.objects.exists())

    def test_passages_behind_their_source_are_not_searched_until_backfilled(self):
        entry = self.source()
        KnowledgeEntry.objects.filter(pk=entry.pk).update(chunked_digest="")
        self.assertEqual(chunks.candidates(self.app, "queue routing"), [])
        self.assertEqual(chunks.behind().count(), 1)
        self.assertEqual(chunks.backfill(), 1)
        self.assertEqual(chunks.behind().count(), 0)
        self.assertTrue(chunks.candidates(self.app, "queue routing"))

    def test_a_restricted_source_is_never_a_candidate(self):
        entry = self.source()
        KnowledgeEntry.objects.filter(pk=entry.pk).update(sensitivity="restricted")
        self.assertTrue(KnowledgeChunk.objects.filter(entry=entry).exists(), "kept for lifting")
        self.assertEqual(chunks.candidates(self.app, "queue routing"), [])

    def test_another_applications_passages_are_never_candidates(self):
        self.source()
        self.assertEqual(chunks.candidates(self.other, "queue routing"), [])

    def test_candidates_do_not_read_the_whole_source(self):
        self.source()
        found = chunks.candidates(self.app, "queue routing")
        self.assertIn("content", found[0].entry.get_deferred_fields())

    def test_passage_answers_rank_as_source_answers_do(self):
        self.source()
        self.source("Database", "Connection pool limits and failover for the primary database.")
        self.source("Queues", "Queue depth alarms page the on-call engineer.")
        for question in ("queue depth", "database failover", "node routing line"):
            with self.subTest(question=question):
                by_source = retrieve(self.app, question, self.owner, candidates="sources")
                by_passage = retrieve(self.app, question, self.owner, candidates="chunks")
                self.assertEqual(
                    [(entry.pk, text) for _, entry, text in by_passage],
                    [(entry.pk, text) for _, entry, text in by_source],
                )

    def test_source_permissions_still_apply_on_the_passage_path(self):
        entry = self.source()
        KnowledgeEntry.objects.filter(pk=entry.pk).update(access_principals=[str(self.owner.pk)])
        self.assertTrue(retrieve(self.app, "queue routing", self.owner, candidates="chunks"))
        self.assertEqual(retrieve(self.app, "queue routing", self.viewer, candidates="chunks"), [])
        self.assertEqual(retrieve(self.app, "queue routing", None, candidates="chunks"), [])

    def test_the_deployment_setting_chooses_the_path(self):
        self.source()
        with (
            override_settings(RETRIEVAL_CANDIDATES="chunks"),
            patch.object(chunks, "candidates", wraps=chunks.candidates) as used,
        ):
            retrieve(self.app, "queue routing", self.owner)
        used.assert_called_once()

    def test_auto_answers_from_passages_only_once_every_source_has_them(self):
        """A source with no current passages is not among them, so "auto" must
        not answer from passages while the backfill is still behind."""
        self.source()
        with (
            override_settings(RETRIEVAL_CANDIDATES="auto"),
            patch.object(chunks, "candidates", wraps=chunks.candidates) as used,
        ):
            retrieve(self.app, "queue routing", self.owner)
            used.assert_called_once()
            KnowledgeEntry.objects.filter(application=self.app).update(chunked_digest="")
            found = retrieve(self.app, "queue routing", self.owner)
            used.assert_called_once()
        self.assertTrue(found, "the source path still finds the source")

    def test_completeness_is_per_application(self):
        self.source()
        self.assertTrue(chunks.complete(self.app))
        KnowledgeEntry.objects.filter(application=self.app).update(chunked_digest="")
        self.assertFalse(chunks.complete(self.app))

    def test_the_command_writes_what_is_behind_and_checks(self):
        self.source()
        KnowledgeEntry.objects.update(chunked_digest="")
        with self.assertRaises(CommandError):
            call_command("chunk_sources", check=True, stdout=io.StringIO())
        output = io.StringIO()
        call_command("chunk_sources", stdout=output)
        self.assertIn("1 sources", output.getvalue())
        call_command("chunk_sources", check=True, stdout=io.StringIO())

    def test_the_maintenance_step_says_when_it_worked(self):
        self.source()
        self.assertFalse(chunks.backfill_step())
        KnowledgeEntry.objects.update(chunked_digest="")
        with self.assertLogs("platform_core.chunks", "INFO"):
            self.assertTrue(chunks.backfill_step())


class RetrievalSettingTests(SimpleTestCase):
    BASE = 'mode = "development"\nhosts = ["localhost"]\nsecret_directory = "/tmp/secrets"\n'

    def load(self, body):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(self.BASE + body, encoding="utf-8")
            with patch.dict("os.environ", {"DIGITAL_BRAIN_CONFIG": str(path)}):
                return load_config()

    def test_only_the_two_paths_are_accepted(self):
        with self.assertRaises(ImproperlyConfigured):
            self.load('retrieval_candidates = "vectors"\n')
        loaded = self.load('retrieval_candidates = "chunks"\n')
        self.assertEqual(loaded["retrieval_candidates"], "chunks")
