"""The kinds of source Knowledge reads are one registry.

The Knowledge tabs and the cycle's Feed stage are drawn from
`source_kinds.KINDS`, so a new kind is one entry and its page, with no change to
the navigation. These pin the registry's order and gating, that the tabs follow
it, and that code is never counted as something the knowledge graph reads.
"""

from dataclasses import replace
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from platform_core import source_kinds
from platform_core.models import ApplicationFeature
from platform_core.source_kinds import KINDS, Kind, visible

from .test_documents import DocumentTests


@override_settings(
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
)
class SourceKindTests(TestCase):
    def setUp(self):
        DocumentTests.setUp(self)

    def tabs(self, name="documents"):
        body = self.client.get(reverse(name, args=[self.app.pk])).content.decode()
        return body.split('aria-label="Knowledge views"')[1].split("</nav>")[0]

    def test_the_kinds_come_in_tab_order_and_each_has_a_page(self):
        self.assertEqual(list(KINDS), ["documents", "code", "tickets"])
        for kind in KINDS.values():
            with self.subTest(kind=kind.key):
                reverse(kind.route, args=[self.app.pk])

    def test_code_is_a_source_the_knowledge_graph_never_reads(self):
        self.assertEqual(
            [kind.key for kind in KINDS.values() if not kind.feeds_graph], ["code"]
        )

    def test_each_kind_is_gated_on_its_own_feature(self):
        self.assertEqual([kind.key for kind in visible(self.app)], ["documents", "code"])
        ApplicationFeature.objects.create(application=self.app, key="code_graph", enabled=False)
        self.assertEqual([kind.key for kind in visible(self.app)], ["documents"])
        ApplicationFeature.objects.filter(key="code_graph").delete()
        ApplicationFeature.objects.create(application=self.app, key="knowledge", enabled=False)
        self.assertEqual([kind.key for kind in visible(self.app)], ["code"])

    def test_a_new_kind_is_one_entry_and_the_tabs_follow(self):
        """What Phase 5's Learned inbox relies on: no navigation code to write."""
        extra = Kind(
            "notes", "Notes", ("note", "notes"), "knowledge", "document", "documents", ("notes",)
        )
        with patch.dict(source_kinds.KINDS, {"notes": extra}):
            tabs = self.tabs()
        self.assertIn("Notes</a>", tabs)
        self.assertLess(tabs.index("Code Repos"), tabs.index("Notes"))

    def test_a_tab_is_current_on_every_page_its_kind_names(self):
        documents = replace(KINDS["documents"], views=("sources", "documents", "graph"))
        with patch.dict(source_kinds.KINDS, {"documents": documents}):
            tabs = self.tabs("graph")
        current = tabs.split('aria-current="page"')[1].split("</a>")[0]
        self.assertTrue(current.endswith("Documents"), current)

    def test_counts_read_like_a_sentence(self):
        self.assertEqual(KINDS["code"].counted(1), "1 repository")
        self.assertEqual(KINDS["code"].counted(3), "3 repositories")
        self.assertEqual(KINDS["documents"].counted(2), "2 documents")
