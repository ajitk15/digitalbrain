import uuid
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from platform_core.code_factory import code_context_for, run_triage
from platform_core.code_graph_analysis import facts, relationships
from platform_core.code_graph_ingest import index_repository, register, showcase_repository
from platform_core.models import (
    ApplicationFeature,
    ApplicationGrant,
    CodeFile,
    CodeRepository,
    CodeSnapshot,
    FactoryRun,
)

from .test_documents import DocumentTests


@override_settings(
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class CodeGraphTests(TestCase):
    def setUp(self):
        DocumentTests.setUp(self)

    def test_register_is_application_scoped_and_rejects_viewers(self):
        repository = register(self.owner, self.app.pk, "Acme/Widgets")
        self.assertEqual(repository.external_id, "acme/widgets")
        self.client.force_login(self.viewer)
        response = self.client.post(
            reverse("code-graph", args=[self.app.pk]),
            {"action": "register", "repository": "acme/other"},
        )
        self.assertEqual(response.status_code, 403)
        self.client.force_login(self.admin)
        response = self.client.get(reverse("code-graph", args=[self.app.pk]))
        self.assertEqual(response.status_code, 404)

    def test_analysis_resolves_project_imports_without_basename_guessing(self):
        analyzed = [
            facts("app/service.py", "from app import helpers\n\ndef run():\n    pass\n"),
            facts("app/helpers.py", "def useful():\n    pass\n"),
            facts("other/helpers.py", "def unrelated():\n    pass\n"),
            facts("web/main.ts", "import { api } from './api'\nexport function start() {}\n"),
            facts("web/api.ts", "export function api() {}\n"),
        ]
        edges = {(edge["source"], edge["target"]) for edge in relationships(analyzed)}
        self.assertIn(("app/service.py", "app/helpers.py"), edges)
        self.assertNotIn(("app/service.py", "other/helpers.py"), edges)
        self.assertIn(("web/main.ts", "web/api.ts"), edges)

    def test_imported_repository_is_showcased_without_automatic_indexing(self):
        repository = showcase_repository(self.owner, self.app, "acme/widgets", "develop")
        self.assertEqual(repository.status, "documentation")
        self.assertEqual(repository.default_ref, "develop")
        self.assertFalse(repository.snapshots.exists())

    @patch("platform_core.code_graph_ingest.github_token", return_value="")
    @patch("platform_core.code_graph_ingest.api_json")
    def test_index_pins_commit_and_builds_explainable_relationships(self, api_json, _token):
        responses = {
            "/repos/acme/widgets": {"default_branch": "main"},
            "/repos/acme/widgets/commits/main": {"sha": "c" * 40},
            f"/repos/acme/widgets/git/trees/{'c' * 40}?recursive=1": {
                "truncated": False,
                "tree": [
                    {"path": "app/main.py", "type": "blob", "size": 30, "sha": "a" * 40},
                    {"path": "app/util.py", "type": "blob", "size": 20, "sha": "b" * 40},
                ],
            },
            f"/repos/acme/widgets/git/blobs/{'a' * 40}": {
                "encoding": "base64", "content": "ZnJvbSBhcHAgaW1wb3J0IHV0aWwK"
            },
            f"/repos/acme/widgets/git/blobs/{'b' * 40}": {
                "encoding": "base64", "content": "ZGVmIGhlbHBlcigpOgogICAgcGFzcwo="
            },
        }
        api_json.side_effect = lambda path, token: responses[path]
        repository = register(self.owner, self.app.pk, "acme/widgets")
        self.assertTrue(index_repository(repository))
        repository.refresh_from_db()
        snapshot = repository.snapshots.get()
        self.assertEqual(repository.status, "ready")
        self.assertEqual(snapshot.commit_sha, "c" * 40)
        self.assertTrue(snapshot.complete)
        self.assertEqual(snapshot.files.count(), 2)
        edge = snapshot.relationships.select_related("source", "target").get()
        self.assertEqual((edge.source.path, edge.target.path), ("app/main.py", "app/util.py"))
        self.assertEqual(edge.confidence, "static")
        response = self.client.get(
            reverse("code-graph", args=[self.app.pk]), {"repository": repository.pk}
        )
        self.assertContains(response, "snapshot v1")
        self.assertContains(response, "app/main.py")

    def test_file_detail_cannot_cross_application_boundary(self):
        repository = CodeRepository.objects.create(
            application=self.other, added_by=self.owner, external_id="acme/private",
            name="acme/private", source_url="https://github.com/acme/private", status="ready",
        )
        snapshot = CodeSnapshot.objects.create(
            repository=repository, number=1, ref="main", commit_sha="c" * 40,
            manifest_digest="m" * 64,
        )
        file = CodeFile.objects.create(
            snapshot=snapshot, path="secret.py", language="python", digest="d" * 64,
            content="secret = True", lines=1,
        )
        self.assertEqual(
            self.client.get(reverse("code-file", args=[self.app.pk, file.pk])).status_code, 404
        )

    @patch("platform_core.code_graph_ingest.github_token", return_value="")
    @patch("platform_core.code_graph_ingest.api_json")
    def test_superseded_index_cannot_publish_its_snapshot(self, api_json, _token):
        repository = register(self.owner, self.app.pk, "acme/widgets")

        def response(path, token):
            if path.endswith("/widgets"):
                return {"default_branch": "main"}
            if "/commits/" in path:
                return {"sha": "c" * 40}
            if "/git/trees/" in path:
                return {
                    "truncated": False,
                    "tree": [{"path": "app.py", "type": "blob", "size": 10, "sha": "a" * 40}],
                }
            CodeRepository.objects.filter(pk=repository.pk).update(
                status="queued", job_id=uuid.uuid4()
            )
            return {"encoding": "base64", "content": "cHJpbnQoJ29sZCcpCg=="}

        api_json.side_effect = response
        self.assertFalse(index_repository(repository))
        self.assertFalse(CodeSnapshot.objects.filter(repository=repository).exists())

    @patch("platform_core.code_factory.ask")
    def test_triage_pins_matching_snapshot_for_code_factory(self, ask):
        repository = CodeRepository.objects.create(
            application=self.app, added_by=self.owner, external_id="acme/widgets",
            name="acme/widgets", source_url="https://github.com/acme/widgets", status="ready",
        )
        snapshot = CodeSnapshot.objects.create(
            repository=repository, number=2, ref="main", commit_sha="c" * 40,
            manifest_digest="m" * 64,
        )
        CodeFile.objects.create(
            snapshot=snapshot, path="billing/service.py", language="python", digest="d" * 64,
            content="def charge():\n    pass", lines=2,
            symbols=[{"name": "charge", "kind": "function", "line": 1}],
        )
        run = FactoryRun.objects.create(application=self.app, requested_by=self.owner)
        ask.return_value = (
            '{"kind":"enhancement","summary":"Billing","requirements":[], '
            '"repository":"acme/widgets"}'
        )
        run_triage(run, "Improve billing")
        run.refresh_from_db()
        self.assertEqual(run.code_snapshot, snapshot)
        context = code_context_for(run, "Update charge")
        self.assertIn("billing/service.py", context)
        self.assertIn("commit " + "c" * 40, context)

    def _pinned_run(self):
        repository = CodeRepository.objects.create(
            application=self.app, added_by=self.owner, external_id="acme/widgets",
            name="acme/widgets", source_url="https://github.com/acme/widgets", status="ready",
        )
        snapshot = CodeSnapshot.objects.create(
            repository=repository, number=1, ref="main", commit_sha="c" * 40,
            manifest_digest="m" * 64,
        )
        CodeFile.objects.create(
            snapshot=snapshot, path="billing/service.py", language="python", digest="d" * 64,
            content="def charge():\n    pass", lines=2,
        )
        run = FactoryRun.objects.create(application=self.app, requested_by=self.owner)
        FactoryRun.objects.filter(pk=run.pk).update(code_snapshot=snapshot)
        run.refresh_from_db()
        return run

    def test_withdrawn_code_graph_access_removes_context_without_failing_the_run(self):
        """Code Factory worked before Code Graph existed; it must survive its removal."""
        run = self._pinned_run()
        ApplicationFeature.objects.update_or_create(
            application=self.app, key="code_graph", defaults={"enabled": False}
        )
        self.assertEqual(code_context_for(run, "Update charge"), "")

        ApplicationFeature.objects.filter(application=self.app, key="code_graph").update(
            enabled=True
        )
        self.assertIn("billing/service.py", code_context_for(run, "Update charge"))

        ApplicationGrant.objects.filter(application=self.app, user=self.owner).delete()
        self.assertEqual(code_context_for(run, "Update charge"), "")

    @patch("platform_core.code_factory.ask")
    def test_triage_does_not_pin_a_snapshot_when_the_feature_is_off(self, ask):
        repository = CodeRepository.objects.create(
            application=self.app, added_by=self.owner, external_id="acme/widgets",
            name="acme/widgets", source_url="https://github.com/acme/widgets", status="ready",
        )
        CodeSnapshot.objects.create(
            repository=repository, number=1, ref="main", commit_sha="c" * 40,
            manifest_digest="m" * 64,
        )
        ApplicationFeature.objects.update_or_create(
            application=self.app, key="code_graph", defaults={"enabled": False}
        )
        run = FactoryRun.objects.create(application=self.app, requested_by=self.owner)
        ask.return_value = (
            '{"kind":"fix","summary":"x","requirements":[],"repository":"acme/widgets"}'
        )
        run_triage(run, "Improve billing")
        run.refresh_from_db()
        self.assertIsNone(run.code_snapshot_id)
        self.assertEqual(run.proposed_repository, "acme/widgets")

    def test_unreadable_repository_id_falls_back_instead_of_claiming_emptiness(self):
        repository = CodeRepository.objects.create(
            application=self.app, added_by=self.owner, external_id="acme/widgets",
            name="acme/widgets", source_url="https://github.com/acme/widgets", status="ready",
        )
        for value in ["not-a-uuid", str(uuid.uuid4())]:
            response = self.client.get(
                reverse("code-graph", args=[self.app.pk]), {"repository": value}
            )
            self.assertNotContains(response, "No repositories yet")
            self.assertContains(response, repository.name)

    def test_search_results_are_not_described_as_a_truncated_file_list(self):
        repository = CodeRepository.objects.create(
            application=self.app, added_by=self.owner, external_id="acme/widgets",
            name="acme/widgets", source_url="https://github.com/acme/widgets", status="ready",
        )
        snapshot = CodeSnapshot.objects.create(
            repository=repository, number=1, ref="main", commit_sha="c" * 40,
            manifest_digest="m" * 64,
        )
        CodeFile.objects.bulk_create(
            [
                CodeFile(
                    snapshot=snapshot, path=f"pkg/mod{index}.py", language="python",
                    digest=f"{index:064d}", content="x = 1", lines=1,
                )
                for index in range(210)
            ]
        )
        CodeFile.objects.create(
            snapshot=snapshot, path="needle.py", language="python", digest="e" * 64,
            content="needle = 1", lines=1,
        )
        url = reverse("code-graph", args=[self.app.pk])
        response = self.client.get(url, {"repository": str(repository.pk), "q": "needle"})
        self.assertContains(response, "needle.py")
        self.assertNotContains(response, "Showing the first 200")
        response = self.client.get(url, {"repository": str(repository.pk)})
        self.assertContains(response, "Showing the first 200")

    def test_github_sources_link_to_their_repository_by_path_not_substring(self):
        from platform_core.source_library import repository_of

        self.assertEqual(repository_of("https://github.com/acme/widgets"), "acme/widgets")
        self.assertEqual(
            repository_of("https://github.com/acme/widgets/blob/main/README.md"), "acme/widgets"
        )
        self.assertEqual(
            repository_of("https://raw.githubusercontent.com/acme/widgets/main/README.md"),
            "acme/widgets",
        )
        # Another repository's file path is not this repository.
        self.assertEqual(
            repository_of("https://github.com/other/thing/blob/main/acme/widgets/x.md"),
            "other/thing",
        )
        self.assertEqual(repository_of("https://example.com/acme/widgets"), "")

    @patch("platform_core.code_graph_ingest.github_token", return_value="")
    @patch("platform_core.code_graph_ingest.api_json")
    def test_a_repository_name_is_checked_before_it_becomes_an_api_path(self, api_json, _token):
        from django.core.exceptions import ValidationError as Invalid

        unusable = ["../../etc", "acme/../secret", "acme", "acme/widgets/extra"]
        for value in unusable:
            # Showcasing declines quietly: it runs beside saving a connector.
            self.assertIsNone(showcase_repository(self.owner, self.app, value))
            # Registering is the deliberate act, and says why it refused.
            with self.assertRaises(Invalid):
                register(self.owner, self.app.pk, value)
        self.assertFalse(CodeRepository.objects.filter(application=self.app).exists())

        # A row that reached the table another way still cannot become a path.
        repository = CodeRepository.objects.create(
            application=self.app, added_by=self.owner, external_id="acme/../secret",
            name="acme/../secret", source_url="https://github.com/acme", status="queued",
        )
        self.assertTrue(index_repository(repository))
        repository.refresh_from_db()
        self.assertEqual(repository.status, "failed")
        api_json.assert_not_called()

    def test_a_connector_owner_github_would_reject_does_not_break_saving_it(self):
        self.assertIsNone(showcase_repository(self.owner, self.app, "my_org/widgets"))

    @patch("platform_core.code_graph_ingest.github_token", return_value="")
    @patch("platform_core.code_graph_ingest.api_json")
    def test_a_rate_limit_keeps_the_files_already_read(self, api_json, _token):
        from django.core.exceptions import ValidationError as Invalid

        def response(path, token):
            if path.endswith("/widgets"):
                return {"default_branch": "main"}
            if "/commits/" in path:
                return {"sha": "c" * 40}
            if "/git/trees/" in path:
                return {
                    "truncated": False,
                    "tree": [
                        {"path": "app/a.py", "type": "blob", "size": 10, "sha": "a" * 40},
                        {"path": "app/b.py", "type": "blob", "size": 10, "sha": "b" * 40},
                    ],
                }
            if path.endswith("a" * 40):
                return {"encoding": "base64", "content": "aW1wb3J0IG9z"}
            raise Invalid("GitHub refused the request, which is usually its rate limit.")

        api_json.side_effect = response
        repository = register(self.owner, self.app.pk, "acme/widgets")
        self.assertTrue(index_repository(repository))
        repository.refresh_from_db()
        snapshot = repository.snapshots.get()
        self.assertEqual(repository.status, "partial")
        self.assertFalse(snapshot.complete)
        self.assertEqual([item.path for item in snapshot.files.all()], ["app/a.py"])
        self.assertTrue(any("rate limit" in warning for warning in snapshot.warnings))
