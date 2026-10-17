"""AI runs reuse unchanged sources; auto-refresh is opt-in, owner-only and self-stopping."""

import hashlib
import json
from decimal import Decimal
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from platform_core import graph_ai
from platform_core.graphs import process_next_graph, rebuild, revision_added
from platform_core.models import (
    AIConfiguration,
    ApplicationGrant,
    AuditEvent,
    GraphRevision,
    KnowledgeEntry,
    KnowledgeGraph,
)
from platform_core.workbench import add_knowledge

from . import test_documents


def answer_for(entries_by_id):
    """A stand-in model: one typed relationship from each source's single line."""

    def extract(user, app_id, purpose, question, citations, **options):
        source_id = citations[0]["id"]
        text = entries_by_id()[source_id].content.rstrip(".")
        subject, _, obj = text.partition(" depends on ")
        return json.dumps(
            {
                "relationships": [
                    {
                        "subject": subject,
                        "subject_type": "Component",
                        "relation": "depends on",
                        "object": obj,
                        "object_type": "Component",
                        "source_id": source_id,
                        "quote": text,
                    }
                ]
            }
        )

    return extract


def by_window(user, app_id, purpose, question, citations, **options):
    """A stand-in model that reads only the window it is shown, line by line."""
    found = []
    for line in citations[0]["excerpt"].splitlines():
        text = line.rstrip(".")
        subject, joined, obj = text.partition(" depends on ")
        if joined:
            found.append(
                {
                    "subject": subject,
                    "subject_type": "Component",
                    "relation": "depends on",
                    "object": obj,
                    "object_type": "Component",
                    "source_id": citations[0]["id"],
                    "quote": text,
                }
            )
    return json.dumps({"relationships": found})


class WindowTests(SimpleTestCase):
    def test_windows_cover_the_text_exactly_and_cut_before_a_heading(self):
        text = "# One\n" + "a line of text\n" * 6 + "# Two\n" + "more text here\n" * 6
        with patch.object(graph_ai, "MAX_SOURCE_CHARACTERS", 120):
            pieces, unread = graph_ai.windows(text)
        self.assertEqual("".join(pieces), text)
        self.assertEqual(unread, 0)
        self.assertTrue(all(len(piece) <= 120 for piece in pieces))
        self.assertTrue(pieces[1].startswith("# Two"))

    def test_text_past_the_last_window_is_counted_not_read(self):
        text = "x\n" * 100
        with patch.object(graph_ai, "MAX_SOURCE_CHARACTERS", 20), patch.object(
            graph_ai, "MAX_WINDOWS", 2
        ):
            pieces, unread = graph_ai.windows(text)
        self.assertEqual(len(pieces), 2)
        self.assertEqual(len("".join(pieces)) + unread, len(text))

    def test_a_short_source_is_one_window_as_it_always_was(self):
        text = "Alpha depends on Queue."
        self.assertEqual(graph_ai.windows(text), ([text], 0))


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class IncrementalTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.alpha = add_knowledge(self.owner, self.app.pk, "Alpha", "Alpha depends on Queue.")
        self.beta = add_knowledge(self.owner, self.app.pk, "Beta", "Beta depends on Store.")
        AIConfiguration.objects.create(
            application=self.app,
            purpose="graph_generation",
            provider="claude",
            model="claude-sonnet-5",
            input_rate=Decimal("2"),
            output_rate=Decimal("10"),
            configured_by=self.owner,
        )
        self.extract = answer_for(
            lambda: {str(e.pk): e for e in KnowledgeEntry.objects.filter(application=self.app)}
        )

    def request(self):
        graph, _ = KnowledgeGraph.objects.get_or_create(application=self.app)
        KnowledgeGraph.objects.filter(pk=graph.pk).update(
            status="queued",
            fingerprint="",
            requested_provider="claude",
            requested_model="claude-sonnet-5",
            requested_by=self.owner,
        )

    def run_ai(self):
        self.request()
        with patch("platform_core.ai.invoke_ai", side_effect=self.extract) as invoke:
            rebuild(self.app.pk)
        return invoke, GraphRevision.objects.order_by("-number").first()

    def change(self, entry, content):
        KnowledgeEntry.objects.filter(pk=entry.pk).update(
            content=content, digest=hashlib.sha256(content.encode()).hexdigest()
        )

    def test_an_unchanged_source_is_not_paid_for_twice(self):
        first, revision = self.run_ai()
        self.assertEqual(first.call_count, 2)
        self.assertEqual(set(revision.data["ai_sources"]), {str(self.alpha.pk), str(self.beta.pk)})
        self.change(self.beta, "Beta depends on Cache.")
        second, revision = self.run_ai()
        # Only the changed source went to the model.
        self.assertEqual(second.call_count, 1)
        self.assertEqual(second.call_args.args[4][0]["id"], str(self.beta.pk))
        self.assertEqual(revision.quality["reused_sources"], 1)
        self.assertEqual(revision.quality["reused_relationships"], 1)
        labels = {n["label"] for n in revision.data["nodes"] if n["kind"] == "entity"}
        self.assertEqual(labels, {"Alpha", "Queue", "Beta", "Cache"})

    def test_nothing_changed_means_no_model_call(self):
        self.run_ai()
        again, revision = self.run_ai()
        self.assertEqual(again.call_count, 0)
        self.assertEqual(revision.quality["reused_sources"], 2)

    def test_a_reused_relationship_is_still_verified_when_cited(self):
        _, revision = self.run_ai()
        revision.published_at = timezone.now()
        revision.save()
        cited = graph_ai.graph_citations(self.app.pk, "What does Alpha depend on?")
        self.assertIn(str(self.alpha.pk), {c["id"] for c in cited})

    def test_another_ontology_version_is_extracted_again(self):
        self.run_ai()
        with patch.object(graph_ai, "ONTOLOGY_VERSION", 99):
            again, _ = self.run_ai()
        self.assertEqual(again.call_count, 2)

    def test_a_source_cut_short_by_the_cap_is_asked_again(self):
        with patch.object(graph_ai, "MAX_RELATIONSHIPS", 1):
            _, revision = self.run_ai()
        self.assertEqual(len(revision.data["ai_sources"]), 1)
        again, revision = self.run_ai()
        self.assertEqual(again.call_count, 1)
        self.assertEqual(len(revision.data["ai_sources"]), 2)

    def test_an_answer_past_the_cap_is_kept_whole_and_not_paid_for_again(self):
        twice = add_knowledge(
            self.owner, self.app.pk, "Delta", "Delta depends on Queue.\nDelta depends on Store."
        )
        KnowledgeEntry.objects.filter(pk__in=[self.alpha.pk, self.beta.pk]).update(active=False)
        with patch.object(graph_ai, "MAX_RELATIONSHIPS", 1):
            first, revision = self.run_ai_by_window()
        self.assertEqual(first.call_count, 1)
        # Both relationships of the one answer, not the first one only.
        self.assertEqual(revision.quality["semantic_relationships"], 2)
        self.assertEqual(set(revision.data["ai_sources"]), {str(twice.pk)})
        again, _ = self.run_ai_by_window()
        self.assertEqual(again.call_count, 0)

    def test_a_paid_answer_survives_a_run_whose_version_is_not_saved(self):
        """A source changing mid-run used to throw away every call already paid for."""

        def extract(*args, **kwargs):
            # A connector sync lands while the model is answering.
            if not KnowledgeEntry.objects.filter(title="Gamma").exists():
                add_knowledge(self.owner, self.app.pk, "Gamma", "Gamma depends on Alpha.")
            return self.extract(*args, **kwargs)

        self.request()
        with patch("platform_core.ai.invoke_ai", side_effect=extract) as invoke:
            self.assertFalse(rebuild(self.app.pk))
        self.assertEqual(invoke.call_count, 2)
        self.assertFalse(GraphRevision.objects.exists())
        # The worker's next rebuild is not a requested run, and calls nothing.
        with patch("platform_core.ai.invoke_ai") as invoke:
            self.assertTrue(rebuild(self.app.pk))
        invoke.assert_not_called()
        revision = GraphRevision.objects.get()
        labels = {n["label"] for n in revision.data["nodes"] if n["kind"] == "entity"}
        self.assertEqual(labels, {"Alpha", "Queue", "Beta", "Store"})
        self.assertEqual(revision.model, "claude-sonnet-5")
        self.assertTrue(revision.quality["carried_forward"])
        self.assertEqual(revision.quality["pending_sources"], 1)

    def test_a_background_rebuild_keeps_the_ai_relationships(self):
        self.run_ai()
        add_knowledge(self.owner, self.app.pk, "Gamma", "Gamma depends on Alpha.")
        with patch("platform_core.ai.invoke_ai") as invoke:
            rebuild(self.app.pk)
        invoke.assert_not_called()
        revision = GraphRevision.objects.order_by("-number").first()
        self.assertEqual(revision.quality["semantic_relationships"], 2)
        self.assertEqual(revision.quality["reused_relationships"], 2)
        self.assertEqual(revision.quality["extraction_calls"], 0)
        self.assertIn("No model was called", " ".join(revision.quality["warnings"]))
        # Carried forward is still verified when cited, like any relationship.
        revision.published_at = timezone.now()
        revision.save()
        cited = graph_ai.graph_citations(self.app.pk, "What does Beta depend on?")
        self.assertIn("Beta depends on Store", " ".join(c["relation"] for c in cited))

    def test_with_nothing_cached_a_rebuild_is_structure_alone(self):
        with patch("platform_core.ai.invoke_ai") as invoke:
            rebuild(self.app.pk)
        invoke.assert_not_called()
        revision = GraphRevision.objects.get()
        self.assertEqual(revision.model, "")
        self.assertNotIn("semantic_relationships", revision.quality)

    def test_the_same_text_under_a_new_record_is_not_paid_for_again(self):
        """A superseded record is a new entry; its unchanged text is the same answer."""
        self.run_ai()
        KnowledgeEntry.objects.filter(pk=self.alpha.pk).update(active=False)
        again = add_knowledge(self.owner, self.app.pk, "Alpha", "Alpha depends on Queue.")
        invoke, revision = self.run_ai()
        self.assertEqual(invoke.call_count, 0)
        self.assertIn(str(again.pk), revision.data["ai_sources"])
        kept = [e for e in revision.data["edges"] if e.get("inferred")]
        self.assertIn(str(again.pk), {e["knowledge_id"] for e in kept})

    def test_an_answer_naming_another_source_is_refused(self):
        def extract(user, app_id, purpose, question, citations, **options):
            answer = json.loads(self.extract(user, app_id, purpose, question, citations))
            other = self.beta if citations[0]["id"] == str(self.alpha.pk) else self.alpha
            for item in answer["relationships"]:
                item["source_id"] = str(other.pk)
            return json.dumps(answer)

        self.request()
        with patch("platform_core.ai.invoke_ai", side_effect=extract):
            rebuild(self.app.pk)
        revision = GraphRevision.objects.get()
        self.assertEqual(revision.quality["semantic_relationships"], 0)
        self.assertEqual(revision.quality["rejection_reasons"], {"unknown source": 2})

    def run_ai_by_window(self):
        """`run_ai` with a model that answers from the text it was shown."""
        self.request()
        with patch("platform_core.ai.invoke_ai", side_effect=by_window) as invoke:
            rebuild(self.app.pk)
        return invoke, GraphRevision.objects.order_by("-number").first()

    def test_a_long_source_is_read_to_the_end_in_windows(self):
        KnowledgeEntry.objects.filter(pk__in=[self.alpha.pk, self.beta.pk]).update(active=False)
        # Distinct names, each line shorter than half a window, so every cut
        # falls at a line end and no two names are close enough to merge.
        pairs = zip(
            ["Alder", "Bravo", "Cedar", "Delta", "Echo", "Foxtrot"],
            ["Queue", "Ledger", "Mailer", "Search", "Billing", "Store"],
            strict=True,
        )
        lines = [f"{a} depends on {b}." for a, b in pairs]
        long = add_knowledge(self.owner, self.app.pk, "Long", "\n".join(lines))
        with patch.object(graph_ai, "MAX_SOURCE_CHARACTERS", 60):
            first, revision = self.run_ai_by_window()
            self.assertGreater(first.call_count, 1)
            labels = {n["label"] for n in revision.data["nodes"] if n["kind"] == "entity"}
            # The last line too, which a single 60-character read never reached.
            self.assertIn("Store", labels)
            self.assertEqual(revision.quality["unread_characters"], 0)
            self.assertIn(str(long.pk), revision.data["ai_sources"])
            # An edit at the end asks again only for the window it is in.
            self.change(long, "\n".join(lines[:-1] + ["Foxtrot depends on Gateway."]))
            again, revision = self.run_ai_by_window()
        self.assertEqual(again.call_count, 1)
        labels = {n["label"] for n in revision.data["nodes"] if n["kind"] == "entity"}
        self.assertIn("Gateway", labels)
        self.assertNotIn("Store", labels)

    def test_sources_added_since_publishing_are_counted(self):
        _, revision = self.run_ai()
        self.assertEqual(revision_added(revision, self.app.pk), 0)
        add_knowledge(self.owner, self.app.pk, "Gamma", "Gamma depends on Alpha.")
        self.assertEqual(revision_added(revision, self.app.pk), 1)
        revision.published_at = timezone.now()
        revision.save()
        response = self.client.get(reverse("graph", args=[self.app.pk]) + "?tab=versions")
        self.assertContains(response, "1 source(s) were added")


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class AutoRefreshTests(TestCase):
    setUp = IncrementalTests.setUp

    def switch(self, user=None, enabled=True):
        self.client.force_login(
            user or self.owner, backend="django.contrib.auth.backends.ModelBackend"
        )
        values = {"action": "auto_refresh"}
        if enabled:
            values["enabled"] = "on"
        return self.client.post(reverse("graph-generate", args=[self.app.pk]), values)

    def settle(self):
        """The background worker, run until it has nothing left to do."""
        with patch("platform_core.ai.invoke_ai", side_effect=self.extract) as invoke:
            for _ in range(10):
                if not process_next_graph():
                    break
            else:
                self.fail("The graph worker never settled.")
        return invoke

    def test_off_by_default_a_source_change_spends_nothing(self):
        self.settle()
        add_knowledge(self.owner, self.app.pk, "Gamma", "Gamma depends on Alpha.")
        self.assertEqual(self.settle().call_count, 0)

    def test_on_a_change_runs_an_incremental_ai_generation_as_its_owner(self):
        self.settle()
        self.assertEqual(self.switch().status_code, 302)
        add_knowledge(self.owner, self.app.pk, "Gamma", "Gamma depends on Alpha.")
        invoke = self.settle()
        self.assertGreaterEqual(invoke.call_count, 1)
        self.assertTrue(all(call.args[0] == self.owner for call in invoke.call_args_list))
        latest = GraphRevision.objects.order_by("-number").first()
        self.assertEqual(latest.model, "claude-sonnet-5")
        self.assertIsNone(latest.published_at)
        self.assertTrue(
            AuditEvent.objects.filter(
                action="graph.generation_requested", details__automatic=True
            ).exists()
        )
        # Nothing changed since: the worker is quiet and spends nothing.
        self.assertEqual(self.settle().call_count, 0)

    def test_only_an_owner_switches_it(self):
        self.assertEqual(self.switch(user=self.viewer).status_code, 403)
        self.assertIsNone(KnowledgeGraph.objects.filter(application=self.app).first())

    def test_losing_access_stops_it_and_says_why(self):
        self.settle()
        self.switch()
        ApplicationGrant.objects.filter(application=self.app, user=self.owner).update(role="viewer")
        KnowledgeEntry.objects.filter(pk=self.beta.pk).update(active=False)
        self.assertEqual(self.settle().call_count, 0)
        graph = KnowledgeGraph.objects.get(application=self.app)
        self.assertIsNone(graph.auto_refresh_by)
        self.assertIn("may no longer change", graph.auto_refresh_note)

    def test_switching_off_clears_who_pays(self):
        self.switch()
        self.switch(enabled=False)
        self.assertIsNone(KnowledgeGraph.objects.get(application=self.app).auto_refresh_by)
