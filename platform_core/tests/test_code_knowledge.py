import hashlib
from types import SimpleNamespace

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from platform_core.code_factory_evidence import (
    CODE_EXCERPT_LINES,
    code_citations,
    code_excerpt,
    code_terms,
    relevant_code_files,
)
from platform_core.code_knowledge import links
from platform_core.factory_views import code_source_holds
from platform_core.graphs import build_graph
from platform_core.models import (
    CodeFile,
    CodeRepository,
    CodeSnapshot,
    FeatureSwitch,
    GraphRevision,
)
from platform_core.workbench import add_knowledge

from . import test_documents
from .test_code_factory import SETTINGS


@override_settings(**SETTINGS)
class CodeKnowledgeTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.entry = add_knowledge(
            self.owner, self.app.pk, "Traceability",
            "| Requirement | Implementation |\n| --- | --- |\n"
            "| FR-09 | services/consent.py |\n| BR-05 | FR-09 |",
        )
        data, quality = build_graph([self.entry], 1)
        self.revision = GraphRevision.objects.create(
            application=self.app, number=1, data=data, quality=quality,
            published_at=timezone.now(), fingerprint="test",
        )
        self.node = next(n["id"] for n in data["nodes"]
                         if n["kind"] == "record" and n["label"] == "FR-09")
        self.repository = CodeRepository.objects.create(
            application=self.app, added_by=self.owner, name="CarePath", external_id="acme/care",
            status="ready", source_url="https://github.com/acme/care",
        )
        self.snapshot = CodeSnapshot.objects.create(
            repository=self.repository, number=1, commit_sha="a" * 40,
        )
        self.file = self.code("src/services/consent.py", '"""Consent FR-09."""\n')

    def code(self, path, content):
        return CodeFile.objects.create(
            snapshot=self.snapshot, path=path, content=content,
            digest=hashlib.sha256(content.encode()).hexdigest(), language="python",
        )

    def factory_run(self):
        return SimpleNamespace(
            code_snapshot=self.snapshot, code_snapshot_id=self.snapshot.pk,
            requested_by=self.owner, application_id=self.app.pk, graph_version=1,
        )

    def test_explicit_paths_and_identifiers_carry_both_endpoints(self):
        found, limited = links(self.revision, self.snapshot, node_id=self.node)
        self.assertFalse(limited)
        self.assertEqual({x["reason"] for x in found}, {
            "Source references services/consent.py", "Shared identifier FR-09",
        })
        self.assertTrue(all(x["commit"] == "a" * 40 for x in found))
        self.assertTrue(all(x["source_digest"] == self.entry.digest for x in found))

    def test_ambiguous_suffix_does_not_create_a_path_link(self):
        self.code("other/services/consent.py", "pass\n")
        found, _ = links(self.revision, self.snapshot, node_id=self.node)
        self.assertEqual([x["reason"] for x in found], ["Shared identifier FR-09"])

    def test_changed_or_retired_source_is_not_linked(self):
        self.entry.content += "\nchanged"
        self.entry.save(update_fields=["content"])
        self.assertEqual(links(self.revision, self.snapshot)[0], [])
        self.entry.refresh_from_db()
        self.entry.active = False
        self.entry.save(update_fields=["active"])
        self.assertEqual(links(self.revision, self.snapshot)[0], [])

    def test_code_digest_is_verified(self):
        self.file.content = "changed"
        self.file.save(update_fields=["content"])
        self.assertEqual(links(self.revision, self.snapshot)[0], [])

    def test_cross_application_and_retired_repository_are_excluded(self):
        self.revision.application = self.other
        self.assertEqual(links(self.revision, self.snapshot)[0], [])
        self.revision.application = self.app
        self.repository.retired_at = timezone.now()
        self.assertEqual(links(self.revision, self.snapshot)[0], [])

    def test_related_code_endpoint_and_reverse_navigation(self):
        url = reverse("graph-related-code", args=[self.app.pk, 1])
        response = self.client.get(url, {"node": self.node})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["links"])
        self.assertEqual(self.client.get(url, {"node": "unknown"}).status_code, 404)
        page = self.client.get(reverse("code-file", args=[self.app.pk, self.file.pk]))
        self.assertContains(page, "Related knowledge")
        self.assertContains(page, "Explore in graph v1")
        self.assertContains(page, f"node={self.node}")

    def test_feature_gate_blocks_cross_graph_access(self):
        FeatureSwitch.objects.update_or_create(key="code_graph", defaults={"enabled": False})
        url = reverse("graph-related-code", args=[self.app.pk, 1])
        self.assertEqual(self.client.get(url, {"node": self.node}).status_code, 403)
        self.assertEqual(relevant_code_files(self.factory_run(), "FR-09")[0], [])

    def test_a_linked_file_is_a_candidate_ranked_by_the_question(self):
        keyword = self.code("aaa.py", "# Change keyword result\n")
        files, _ = relevant_code_files(self.factory_run(), "Change BR-05")
        # BR-05's source row mentions FR-09, which points at the consent file:
        # offered with its link though it holds none of the question's words,
        # and after the file that does.
        self.assertEqual([f.pk for f in files], [keyword.pk, self.file.pk])
        self.assertEqual(files[1].knowledge_link["reason"], "Shared identifier FR-09")
        citations = code_citations(self.factory_run(), "Change BR-05")
        self.assertEqual(citations[1]["id"], f"code:{self.file.pk}")
        self.assertEqual(citations[1]["knowledge_link"]["revision"], 1)

    def test_files_rank_by_the_question_not_by_path(self):
        self.code("a/first.py", "# the search\n")
        holds = self.code("z/last.py", "def search(limit):\n    return clamp(limit)\n")
        files, terms = relevant_code_files(
            self.factory_run(), "The search passes the limit unclamped"
        )
        self.assertEqual(files[0].pk, holds.pk)
        self.assertNotIn("the", terms)

    def test_an_excerpt_centres_on_the_line_holding_most_terms(self):
        lines = ["import search\n"] + [f"x{n} = {n}\n" for n in range(30)]
        lines[22] = "rows = search(name, limit)  # unbounded limit\n"
        content = "".join(lines)
        excerpt, first = code_excerpt(content, code_terms("search limit unbounded"))
        self.assertIn(lines[22].rstrip(), excerpt)
        self.assertIn(excerpt, content)
        # The window is centred: the line sits half a window from its start.
        self.assertEqual(first, 23 - CODE_EXCERPT_LINES // 2)
        self.assertNotIn("import search", excerpt)
        # At the end of a file the window stays whole rather than running off it.
        _, last = code_excerpt(content + "limit search tail\n", ["limit", "search", "tail"])
        self.assertEqual(last, len(lines) + 2 - CODE_EXCERPT_LINES)

    def test_factory_never_uses_a_different_commit(self):
        old = self.snapshot
        self.snapshot = CodeSnapshot.objects.create(
            repository=self.repository, number=2, commit_sha="b" * 40,
        )
        newer = self.code("src/services/consent.py", "# FR-09\n")
        self.snapshot = old
        files, _ = relevant_code_files(self.factory_run(), "FR-09")
        self.assertNotIn(newer.pk, [f.pk for f in files])

    def test_linked_citation_fails_review_after_source_changes(self):
        citation = code_citations(self.factory_run(), "FR-09")[0]
        self.assertTrue(code_source_holds(self.app, citation))
        self.entry.active = False
        self.entry.save(update_fields=["active"])
        self.assertFalse(code_source_holds(self.app, citation))
