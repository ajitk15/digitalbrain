"""The knowledge cycle, shown: stages on onboarding, a strip on Knowledge, Attention.

`lifecycle` reads what the platform already keeps - sources, versions, the
published one, benchmark runs, origins - and says where an application stands.
These pin what each reader says, that only someone who can act is told, and that
nothing here writes.
"""

import hashlib

from django.test import TestCase, override_settings
from django.urls import reverse

from platform_core.attention import items
from platform_core.graphs import build_graph, fingerprint
from platform_core.lifecycle import DONE, TODO, WARN, stages, summary
from platform_core.models import (
    ApplicationFeature,
    ApplicationGrant,
    BenchmarkRun,
    CodeRepository,
    GraphRevision,
    KnowledgeEntry,
    KnowledgeSource,
)

from .test_documents import DocumentTests


@override_settings(
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class LifecycleTests(TestCase):
    def setUp(self):
        DocumentTests.setUp(self)

    def entry(self, title="Runbook", content="Restart the export worker when it stalls."):
        return KnowledgeEntry.objects.create(
            application=self.app,
            author=self.owner,
            title=title,
            content=content,
            digest=hashlib.sha256(content.encode()).hexdigest(),
        )

    def version(self, number, published=False, current=True):
        entries = list(KnowledgeEntry.objects.filter(application=self.app, active=True))
        data, quality = build_graph(entries, len(entries))
        return GraphRevision.objects.create(
            application=self.app,
            number=number,
            fingerprint=fingerprint(self.app.pk) if current else "0" * 64,
            data=data,
            quality=quality,
            published_at="2026-10-01T00:00:00Z" if published else None,
        )

    def scored(self, revision, found, questions=4):
        return BenchmarkRun.objects.create(
            application=self.app,
            revision=revision,
            questions=questions,
            found=found,
            top_three=found,
            reciprocal_rank=found / questions,
        )

    def state(self, key):
        return {stage.key: stage for stage in stages(self.app)}[key]

    # ---- the stages ----

    def test_a_new_application_has_every_stage_to_do_in_order(self):
        found = stages(self.app)
        self.assertEqual(
            [stage.key for stage in found], ["setup", "feed", "build", "check", "publish", "use"]
        )
        self.assertEqual({stage.state for stage in found}, {TODO})
        self.assertIn("connectors not chosen", found[0].detail)

    def test_feeding_counts_each_kind(self):
        self.entry()
        KnowledgeEntry.objects.create(
            application=self.app,
            author=self.owner,
            title="CARE-1",
            content="A ticket.",
            source="https://jira.example/browse/CARE-1",
            digest="d" * 64,
        )
        repository = CodeRepository.objects.create(
            application=self.app,
            provider="github",
            external_id="acme/widgets",
            name="acme/widgets",
            source_url="https://github.com/acme/widgets",
            added_by=self.owner,
            status="ready",
        )
        feed = self.state("feed")
        self.assertEqual(feed.state, DONE)
        self.assertIn("1 imported record", feed.detail)
        self.assertIn("1 note", feed.detail)
        self.assertIn("1 repository", feed.detail)
        # A repository whose documents alone were imported has no code to read.
        repository.status = "documentation"
        repository.save(update_fields=["status"])
        self.assertNotIn("repositor", self.state("feed").detail)

    def test_a_draft_newer_than_what_answers_is_ready_to_publish(self):
        self.entry()
        self.version(1, published=True)
        self.entry("Design", "The export service reads consent first.")
        draft = self.version(2)
        state = summary(self.app)
        self.assertEqual(state.draft, draft)
        self.assertFalse(state.behind)
        publish = self.state("publish")
        self.assertEqual(publish.state, WARN)
        self.assertIn("version 2 is ready to publish", publish.detail)

    def test_a_published_version_the_sources_have_moved_past_is_behind(self):
        self.entry()
        self.version(1, published=True)
        self.entry("Design", "The export service reads consent first.")
        state = summary(self.app)
        self.assertIsNone(state.draft)
        self.assertTrue(state.behind)
        self.assertIn("behind its sources", self.state("publish").detail)

    def test_a_draft_that_no_longer_reads_its_sources_is_not_offered(self):
        self.entry()
        self.version(1, published=True)
        self.version(2, current=False)
        self.assertIsNone(summary(self.app).draft)

    def test_check_warns_when_the_newest_version_finds_less_than_the_published(self):
        self.entry()
        published = self.version(1, published=True)
        self.entry("Design", "The export service reads consent first.")
        draft = self.version(2)
        self.scored(published, 4)
        self.scored(draft, 3)
        check = self.state("check")
        self.assertEqual(check.state, WARN)
        self.assertIn("finds 3 of 4", check.detail)
        self.assertIn("published version found 4", check.detail)
        self.scored(draft, 4)
        self.assertEqual(self.state("check").state, DONE)

    def test_use_names_what_reads_the_published_version(self):
        self.entry()
        self.version(1, published=True)
        use = self.state("use")
        self.assertEqual(use.state, DONE)
        self.assertIn("Chat", use.detail)
        self.assertTrue(use.detail.endswith("answer from version 1."), use.detail)

    # ---- moved at the origin ----

    def test_origins_and_repositories_that_moved_are_counted(self):
        for status in ("stale", "ready", "stale"):
            KnowledgeSource.objects.create(
                application=self.app,
                added_by=self.owner,
                url=f"https://example.com/{status}/{KnowledgeSource.objects.count()}",
                name="Docs",
                status=status,
            )
        repository = CodeRepository.objects.create(
            application=self.app,
            provider="github",
            external_id="acme/widgets",
            name="acme/widgets",
            source_url="https://github.com/acme/widgets",
            added_by=self.owner,
            status="ready",
            head_sha="b" * 40,
        )
        repository.snapshots.create(number=1, commit_sha="a" * 40)
        state = summary(self.app)
        self.assertEqual((state.moved, state.moved_code), (2, 1))
        repository.snapshots.create(number=2, commit_sha="b" * 40)
        self.assertEqual(summary(self.app).moved_code, 0)

    # ---- the strip ----

    def test_every_knowledge_page_says_what_answers_and_what_waits(self):
        self.entry()
        self.version(1, published=True)
        self.entry("Design", "The export service reads consent first.")
        self.version(2)
        for name in ("documents", "graph", "code-repositories"):
            with self.subTest(page=name):
                body = self.client.get(reverse(name, args=[self.app.pk])).content.decode()
                self.assertIn('class="knowledge-status"', body)
                self.assertIn("Answering from", body)
                self.assertIn("Version 2 is ready to publish", body)

    def test_the_strip_says_nothing_is_published(self):
        body = self.client.get(reverse("documents", args=[self.app.pk])).content.decode()
        self.assertIn("Nothing published yet", body)
        self.assertNotIn("ready to publish", body)

    def test_without_knowledge_code_repos_has_no_strip(self):
        ApplicationFeature.objects.create(application=self.app, key="knowledge", enabled=False)
        body = self.client.get(reverse("code-repositories", args=[self.app.pk])).content.decode()
        self.assertNotIn('class="knowledge-status"', body)

    # ---- onboarding ----

    def test_onboarding_walks_the_cycle_before_the_gates(self):
        body = self.client.get(reverse("onboarding", args=[self.app.pk])).content.decode()
        self.assertIn("The knowledge cycle", body)
        for label in ("Set up", "Feed", "Build", "Check", "Publish", "Use"):
            self.assertIn(f". {label}</a>", body)
        self.assertLess(body.index("The knowledge cycle"), body.index("Choose your connectors"))

    # ---- attention ----

    def test_an_owner_is_told_a_version_waits_and_a_viewer_is_not(self):
        self.entry()
        self.version(1)
        texts = [item.text for item in items(self.owner, [self.app])]
        # Made directly, so never checked: the plain wording.
        self.assertIn("Version 1 is built and waiting to be published", texts)
        self.assertFalse(
            [item for item in items(self.viewer, [self.app]) if item.kind == "knowledge"]
        )

    def test_a_contributor_is_told_the_published_version_is_behind(self):
        contributor = ApplicationGrant.objects.create(
            application=self.app, user=self.admin, role="contributor"
        ).user
        self.entry()
        self.version(1, published=True)
        self.entry("Design", "The export service reads consent first.")
        knowledge = [item for item in items(contributor, [self.app]) if item.kind == "knowledge"]
        self.assertEqual(
            [item.text for item in knowledge], ["The published version 1 is behind its sources"]
        )
        self.assertEqual(knowledge[0].url, reverse("graph", args=[self.app.pk]))

    def test_the_full_list_filters_to_knowledge(self):
        self.entry()
        self.version(1)
        page = self.client.get(reverse("attention"), {"kind": "knowledge"})
        self.assertContains(page, "Version 1 is built and waiting to be published")
        self.assertContains(page, "Knowledge to publish or resync")

    def test_nothing_waits_once_the_newest_version_is_published(self):
        self.entry()
        self.version(1, published=True)
        self.assertFalse(
            [item for item in items(self.owner, [self.app]) if item.kind == "knowledge"]
        )
