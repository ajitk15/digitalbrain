"""The release check: a version may answer only if it is sound, and a worse one needs a reason.

Hard checks - changed sources, a quote its source does not hold, a restricted
source - refuse publishing outright. Soft checks compare with the version that
answers now and need a typed reason, which is kept on the version and in the
audit event. These pin each check, both ways, and every way of publishing.
"""

import hashlib
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from platform_core.graphs import build_graph, fingerprint, publish_revision, rebuild
from platform_core.models import AuditEvent, BenchmarkRun, GraphRevision, KnowledgeEntry
from platform_core.release import check, publish

from .test_documents import DocumentTests


@override_settings(
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class ReleaseCheckTests(TestCase):
    def setUp(self):
        DocumentTests.setUp(self)
        self.runbook = self.entry("Runbook", "Restart the export worker when it stalls.")
        self.design = self.entry("Design", "The export service reads consent first.")

    def entry(self, title, content):
        return KnowledgeEntry.objects.create(
            application=self.app,
            author=self.owner,
            title=title,
            content=content,
            digest=hashlib.sha256(content.encode()).hexdigest(),
        )

    def version(self, number, published=False, edges=(), quality=None):
        entries = list(KnowledgeEntry.objects.filter(application=self.app, active=True))
        data, built = build_graph(entries, len(entries))
        data["edges"] = data.get("edges", []) + list(edges)
        return GraphRevision.objects.create(
            application=self.app,
            number=number,
            fingerprint=fingerprint(self.app.pk),
            data=data,
            quality={**built, **(quality or {})},
            published_at="2026-10-01T00:00:00Z" if published else None,
        )

    def quoted(self, entry, quote, inferred=True):
        return {
            "source": f"entity:{entry.pk}:a",
            "target": f"entity:{entry.pk}:b",
            "relation": "depends on",
            "knowledge_id": str(entry.pk),
            "digest": entry.digest,
            "evidence": quote,
            "inferred": inferred,
        }

    def scored(self, revision, found, questions=4, structure=None):
        return BenchmarkRun.objects.create(
            application=self.app,
            revision=revision,
            questions=questions,
            found=found,
            top_three=found,
            reciprocal_rank=found / questions,
            structure=structure,
        )

    def failing(self, report):
        return {item.key for item in report.checks if not item.ok}

    # ---- hard: refuse ----

    def test_with_nothing_published_only_the_hard_checks_run_and_a_sound_version_passes(self):
        revision = self.version(1)
        report = check(self.app, revision)
        self.assertEqual(
            [item.key for item in report.checks], ["readable", "citations", "restricted"]
        )
        self.assertTrue(report.passed)
        published, _ = publish(self.owner, self.app, 1)
        self.assertIsNotNone(published.published_at)
        self.assertEqual(published.publish_reason, "")

    def test_a_version_whose_sources_changed_is_refused_whatever_the_reason(self):
        self.version(1)
        self.runbook.active = False
        self.runbook.save(update_fields=["active"])
        with self.assertRaises(ValidationError) as refused:
            publish(self.owner, self.app, 1, reason="Ship it")
        self.assertIn("cannot be published", str(refused.exception))
        self.assertIsNone(GraphRevision.objects.get(number=1).published_at)

    def test_a_quote_its_source_does_not_hold_is_refused(self):
        revision = self.version(1, edges=[self.quoted(self.runbook, "Delete the database.")])
        self.assertEqual(self.failing(check(self.app, revision)), {"citations"})
        with self.assertRaises(ValidationError):
            publish(self.owner, self.app, 1, reason="Anyway")

    def test_an_exact_quote_verifies(self):
        revision = self.version(1, edges=[self.quoted(self.runbook, "Restart the export worker")])
        self.assertTrue(check(self.app, revision).passed)

    def test_a_source_restricted_since_is_refused(self):
        revision = self.version(1)
        KnowledgeEntry.objects.filter(pk=self.design.pk).update(sensitivity="restricted")
        self.assertIn("restricted", self.failing(check(self.app, revision)))

    # ---- soft: compared with what answers, a reason to go over ----

    def test_a_version_missing_a_published_source_needs_a_reason(self):
        self.version(1, published=True)
        self.design.active = False
        self.design.save(update_fields=["active"])
        self.version(2)
        report = check(self.app, GraphRevision.objects.get(number=2))
        self.assertEqual(self.failing(report), {"sources_lost"})
        self.assertIn("Design", report.soft_failures[0].detail)
        with self.assertRaises(ValidationError) as refused:
            publish(self.owner, self.app, 2)
        self.assertIn("Say why", str(refused.exception))
        revision, _ = publish(self.owner, self.app, 2, reason="  Design was retired   on purpose ")
        self.assertEqual(revision.publish_reason, "Design was retired on purpose")

    def test_answering_fewer_benchmark_questions_is_a_warning(self):
        published = self.version(1, published=True)
        draft = self.version(2)
        self.scored(published, 4)
        self.scored(draft, 3)
        self.assertEqual(self.failing(check(self.app, draft)), {"benchmark"})
        self.scored(draft, 4)
        self.assertTrue(check(self.app, draft).passed)

    def test_an_unscored_draft_against_a_scored_version_is_a_warning(self):
        self.scored(self.version(1, published=True), 4)
        self.assertEqual(self.failing(check(self.app, self.version(2))), {"benchmark"})

    def test_ai_that_takes_answers_away_is_a_warning_and_ai_that_adds_nothing_is_not(self):
        self.version(1, published=True)
        draft = self.version(2, edges=[self.quoted(self.runbook, "Restart the export worker")])
        run = self.scored(draft, 3, structure={"found": 4, "reciprocal_rank": 1.0})
        self.assertIn("ai_lift", self.failing(check(self.app, draft)))
        run.structure = {"found": 3, "reciprocal_rank": 0.75}
        run.save(update_fields=["structure"])
        report = check(self.app, draft)
        self.assertNotIn("ai_lift", self.failing(report))
        lift = next(item for item in report.checks if item.key == "ai_lift")
        self.assertIn("added nothing", lift.detail)

    def test_a_larger_share_of_refused_proposals_is_a_warning(self):
        self.version(
            1, published=True, quality={"semantic_relationships": 9, "rejected_relationships": 1}
        )
        draft = self.version(
            2, quality={"semantic_relationships": 5, "rejected_relationships": 5}
        )
        self.assertEqual(self.failing(check(self.app, draft)), {"ontology"})

    def test_a_version_is_never_compared_with_itself(self):
        revision = self.version(1, published=True)
        report = check(self.app, revision)
        self.assertIsNone(report.against)
        self.assertEqual(len(report.checks), 3)

    # ---- stored, and read by the strip ----

    def test_every_saved_version_is_checked_and_the_strip_says_so(self):
        self.version(1, published=True)
        self.entry("Handbook", "Escalate to the on-call engineer.")
        with self.captureOnCommitCallbacks(execute=True):
            self.assertTrue(rebuild(self.app.pk))
        revision = GraphRevision.objects.filter(application=self.app).first()
        self.assertEqual(revision.number, 2)
        self.assertEqual(revision.release["against"], 1)
        self.assertEqual(revision.release_state, "passed")
        body = self.client.get(reverse("documents", args=[self.app.pk])).content.decode()
        self.assertIn("Version 2 passed its release check", body)

    def test_a_failing_check_never_costs_the_version(self):
        with patch("platform_core.release.check", side_effect=RuntimeError("boom")):
            with self.captureOnCommitCallbacks(execute=True):
                self.assertTrue(rebuild(self.app.pk))
        self.assertTrue(GraphRevision.objects.filter(application=self.app).exists())

    # ---- the page, and every way to publish ----

    def test_the_versions_table_opens_the_check(self):
        self.version(1)
        page = self.client.get(reverse("graph", args=[self.app.pk]), {"tab": "versions"})
        self.assertContains(
            page, f'href="{reverse("graph-release", args=[self.app.pk, 1])}" data-modal'
        )
        self.assertNotContains(page, 'value="publish"')

    def test_the_page_publishes_a_sound_version_and_audits_it(self):
        self.version(1)
        url = reverse("graph-release", args=[self.app.pk, 1])
        self.assertContains(self.client.get(url), "Publish version 1")
        response = self.client.post(url)
        self.assertRedirects(
            response,
            f"{reverse('graph', args=[self.app.pk])}?tab=versions",
            fetch_redirect_response=False,
        )
        event = AuditEvent.objects.get(action="graph.published")
        self.assertEqual(event.details, {"version": 1, "against": None})

    def test_the_page_asks_for_a_reason_and_keeps_it_in_the_audit(self):
        self.version(1, published=True)
        self.design.active = False
        self.design.save(update_fields=["active"])
        self.version(2)
        url = reverse("graph-release", args=[self.app.pk, 2])
        page = self.client.get(url)
        self.assertContains(page, "Publish anyway")
        self.assertContains(page, 'name="reason"')
        refused = self.client.post(url, {"reason": ""})
        self.assertEqual(refused.status_code, 200)
        self.assertContains(refused, "Say why it should answer anyway")
        self.assertIsNone(GraphRevision.objects.get(number=2).published_at)
        self.client.post(url, {"reason": "Design was retired on purpose"})
        revision = GraphRevision.objects.get(number=2)
        self.assertIsNotNone(revision.published_at)
        event = AuditEvent.objects.get(action="graph.published")
        self.assertEqual(event.details["warnings"], ["sources_lost"])
        self.assertEqual(event.details["reason"], "Design was retired on purpose")

    def test_a_refused_version_offers_no_publish_button(self):
        self.version(1)
        self.runbook.active = False
        self.runbook.save(update_fields=["active"])
        page = self.client.get(reverse("graph-release", args=[self.app.pk, 1]))
        self.assertContains(page, "cannot be published")
        self.assertNotContains(page, 'type="submit"')

    def test_a_viewer_reads_the_check_and_cannot_publish(self):
        self.version(1)
        self.client.force_login(self.viewer, backend="django.contrib.auth.backends.ModelBackend")
        url = reverse("graph-release", args=[self.app.pk, 1])
        page = self.client.get(url)
        self.assertContains(page, "Release check · version 1")
        self.assertNotContains(page, 'type="submit"')
        self.assertEqual(self.client.post(url).status_code, 403)
        self.assertIsNone(GraphRevision.objects.get(number=1).published_at)

    def test_no_grant_reads_as_not_found(self):
        self.version(1)
        self.client.force_login(self.admin, backend="django.contrib.auth.backends.ModelBackend")
        url = reverse("graph-release", args=[self.app.pk, 1])
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url).status_code, 404)

    def test_another_applications_version_reads_as_not_found(self):
        GraphRevision.objects.create(application=self.other, number=1, fingerprint="f" * 64)
        url = reverse("graph-release", args=[self.other.pk, 1])
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url).status_code, 404)
        self.assertIsNone(GraphRevision.objects.get(application=self.other).published_at)

    def test_the_old_form_and_the_function_hold_the_same_rule(self):
        self.version(1, published=True)
        self.design.active = False
        self.design.save(update_fields=["active"])
        self.version(2)
        graph = reverse("graph", args=[self.app.pk])
        self.client.post(graph, {"action": "publish", "version": "2"})
        self.assertIsNone(GraphRevision.objects.get(number=2).published_at)
        with self.assertRaises(ValidationError):
            publish_revision(self.owner, self.app.pk, 2)
        publish_revision(self.owner, self.app.pk, 2, reason="Retired on purpose")
        self.assertIsNotNone(GraphRevision.objects.get(number=2).published_at)
