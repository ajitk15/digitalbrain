from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.test import TestCase, override_settings

from platform_core.agent_runtime.tools import (
    BUDGET_EXHAUSTED,
    FETCH_SOURCE,
    NOT_FOUND,
    SEARCH_KNOWLEDGE,
    CitationRecorder,
    ToolScope,
    run_tool,
    verify_citations,
)
from platform_core.models import ApplicationGrant, FeatureSwitch
from platform_core.workbench import add_knowledge

from . import test_documents


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class KnowledgeToolTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.source = add_knowledge(
            self.owner, self.app.pk, "Refund policy", "Refund requests expire after thirty days."
        )
        self.scope = ToolScope(user_id=self.owner.pk, app_id=self.app.pk)

    def recorder(self):
        return CitationRecorder(self.scope)

    def test_search_returns_passages_and_their_provenance(self):
        recorder = self.recorder()
        text = run_tool(SEARCH_KNOWLEDGE, self.scope, {"query": "refund"}, recorder)
        self.assertIn("thirty days", text)
        self.assertIn(str(self.source.pk), text)
        self.assertEqual(recorder.verified_citations()[0]["id"], str(self.source.pk))

    def test_search_without_matches_says_so_rather_than_inventing(self):
        recorder = self.recorder()
        text = run_tool(SEARCH_KNOWLEDGE, self.scope, {"query": "zeppelin"}, recorder)
        self.assertIn("No sources", text)
        self.assertEqual(recorder.verified_citations(), [])

    def test_fetch_source_reads_a_bounded_window_of_its_own_application(self):
        recorder = self.recorder()
        text = run_tool(
            FETCH_SOURCE, self.scope, {"source_id": str(self.source.pk)}, recorder
        )
        self.assertIn("Refund requests expire", text)
        self.assertEqual(len(recorder.verified_citations()), 1)

    def test_another_applications_source_id_is_not_found_never_leaked(self):
        ApplicationGrant.objects.create(application=self.other, user=self.owner, role="owner")
        foreign = add_knowledge(
            self.owner, self.other.pk, "Secret policy", "Refund deadline is secret."
        )
        recorder = self.recorder()
        text = run_tool(FETCH_SOURCE, self.scope, {"source_id": str(foreign.pk)}, recorder)
        self.assertEqual(text, NOT_FOUND)
        self.assertNotIn("secret", text.lower())
        self.assertEqual(recorder.verified_citations(), [])
        # Searching must not reach across applications either.
        search = run_tool(SEARCH_KNOWLEDGE, self.scope, {"query": "secret"}, self.recorder())
        self.assertNotIn("Secret policy", search)

    def test_malformed_source_ids_are_answered_not_raised(self):
        for value in ["", "not-a-uuid", None, 17, {"nested": 1}]:
            with self.subTest(value=value):
                text = run_tool(FETCH_SOURCE, self.scope, {"source_id": value}, self.recorder())
                self.assertEqual(text, NOT_FOUND)

    def test_a_revoked_grant_stops_the_next_tool_call(self):
        recorder = self.recorder()
        run_tool(SEARCH_KNOWLEDGE, self.scope, {"query": "refund"}, recorder)
        ApplicationGrant.objects.filter(application=self.app, user=self.owner).delete()
        # Deny-by-default surfaces as Http404 rather than confirming the application
        # exists. Either way the tool fails closed instead of returning data.
        with self.assertRaises(Http404):
            run_tool(SEARCH_KNOWLEDGE, self.scope, {"query": "refund"}, recorder)

    def test_a_disabled_feature_stops_the_next_tool_call(self):
        FeatureSwitch.objects.create(key="knowledge", enabled=False)
        with self.assertRaises(PermissionDenied):
            run_tool(SEARCH_KNOWLEDGE, self.scope, {"query": "refund"}, self.recorder())

    def test_the_tool_budget_halts_further_searching(self):
        recorder = CitationRecorder(self.scope, budget=2)
        for _ in range(2):
            self.assertNotEqual(
                run_tool(SEARCH_KNOWLEDGE, self.scope, {"query": "refund"}, recorder),
                BUDGET_EXHAUSTED,
            )
        self.assertEqual(
            run_tool(SEARCH_KNOWLEDGE, self.scope, {"query": "refund"}, recorder),
            BUDGET_EXHAUSTED,
        )
        self.assertEqual(recorder.calls, 2)


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class CitationVerificationTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.source = add_knowledge(
            self.owner, self.app.pk, "Refund policy", "Refund requests expire after thirty days."
        )

    def citation(self, **overrides):
        return {
            "id": str(self.source.pk),
            "title": self.source.title,
            "digest": self.source.digest,
            "excerpt": "Refund requests expire",
            **overrides,
        }

    def test_a_genuine_citation_survives(self):
        self.assertEqual(len(verify_citations(self.app.pk, [self.citation()])), 1)

    def test_a_fabricated_citation_is_dropped(self):
        candidates = [
            self.citation(excerpt="Refunds are never allowed."),
            self.citation(digest="0" * 64),
            self.citation(id="00000000-0000-0000-0000-000000000000"),
            self.citation(excerpt=""),
        ]
        self.assertEqual(verify_citations(self.app.pk, candidates), [])

    def test_an_archived_source_stops_backing_its_citation(self):
        self.source.active = False
        self.source.save(update_fields=["active"])
        self.assertEqual(verify_citations(self.app.pk, [self.citation()]), [])

    def test_verification_is_scoped_to_the_application(self):
        self.assertEqual(verify_citations(self.other.pk, [self.citation()]), [])

    def test_duplicate_citations_collapse(self):
        self.assertEqual(len(verify_citations(self.app.pk, [self.citation(), self.citation()])), 1)

    def test_citations_are_capped(self):
        recorder = CitationRecorder(ToolScope(user_id=self.owner.pk, app_id=self.app.pk))
        # Distinct excerpts, all genuine, exceeding the cap.
        content = self.source.content
        recorder.seed(
            [self.citation(excerpt=content[index : index + 12]) for index in range(0, 24)]
        )
        self.assertLessEqual(len(recorder.verified_citations()), 8)
