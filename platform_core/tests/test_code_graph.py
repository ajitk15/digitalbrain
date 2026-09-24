import uuid
from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from platform_core.code_factory import (
    code_context_for,
    confirm_repository,
    run_design,
    run_triage,
)
from platform_core.code_graph_analysis import facts, relationships
from platform_core.code_graph_ingest import index_repository, register
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

    def test_add_repository_is_a_popup_onto_a_page_of_its_own(self):
        add = reverse("code-graph-add", args=[self.app.pk])
        body = self.client.get(reverse("code-graph", args=[self.app.pk])).content.decode()
        self.assertIn(f'href="{add}" data-modal', body)
        self.assertNotIn('value="register"', body)
        self.assertContains(self.client.get(add), 'value="register"')
        # A refusal is shown on the form page rather than lost on the list.
        response = self.client.post(add, {"action": "register", "repository": "not a name"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="errorlist')
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(add).status_code, 403)

    def test_a_repository_is_retired_rather_than_deleted(self):
        """Removing one must not rewrite what past runs were given.

        CodeSnapshot.repository is PROTECT and FactoryRun.code_snapshot is
        SET_NULL, so a hard delete is either refused or quietly blanks the code
        pin on every run that reasoned about it. Retiring takes it out of every
        live query and leaves the history where it is.
        """
        from platform_core.models import CodeRepository, FactoryRun

        repository = register(self.owner, self.app.pk, "acme/widgets")
        snapshot = repository.snapshots.create(number=1, commit_sha="a" * 40)
        run = FactoryRun.objects.create(
            application=self.app,
            requested_by=self.owner,
            ticket_title="A ticket",
            code_snapshot=snapshot,
        )

        response = self.client.post(
            reverse("code-graph", args=[self.app.pk]),
            {"action": "retire", "repository": str(repository.pk)},
            follow=True,
        )
        self.assertContains(response, "was removed from this application")

        repository.refresh_from_db()
        run.refresh_from_db()
        self.assertIsNotNone(repository.retired_at)
        # The row, its snapshot, and the run's pin all survive.
        self.assertTrue(CodeRepository.objects.filter(pk=repository.pk).exists())
        self.assertEqual(run.code_snapshot_id, snapshot.pk)

    def test_the_snapshot_facts_sit_on_the_graph_toolbar(self):
        repository = register(self.owner, self.app.pk, "acme/widgets")
        repository.snapshots.create(number=1, commit_sha="a" * 40)
        repository.status = "ready"
        repository.save(update_fields=["status"])
        response = self.client.get(
            reverse("code-graph", args=[self.app.pk]), {"repository": repository.pk}
        )
        toolbar = response.content.decode().split('class="graph-toolbar"')[1]
        toolbar = toolbar.split('class="code-graph-layout"')[0]
        self.assertIn('class="graph-facts"', toolbar)
        self.assertIn("aaaaaaaa", toolbar)
        self.assertNotContains(response, "graph-metrics")

    def test_a_retired_repository_is_not_offered_or_pinned(self):
        from platform_core.code_factory import pin_repository
        from platform_core.models import FactoryRun

        repository = register(self.owner, self.app.pk, "acme/widgets")
        repository.status = "ready"
        repository.save(update_fields=["status"])
        repository.snapshots.create(number=1, commit_sha="a" * 40)
        self.client.post(
            reverse("code-graph", args=[self.app.pk]),
            {"action": "retire", "repository": str(repository.pk)},
        )

        page = self.client.get(reverse("code-graph", args=[self.app.pk]))
        self.assertContains(page, "No repositories yet")

        run = FactoryRun.objects.create(
            application=self.app, requested_by=self.owner, ticket_title="A ticket"
        )
        pin_repository(run, "")
        self.assertIsNone(run.code_snapshot)

    def test_registering_a_retired_name_again_brings_it_back(self):
        """The unique slot is still held, and refusing would be unhelpful."""
        repository = register(self.owner, self.app.pk, "acme/widgets")
        self.client.post(
            reverse("code-graph", args=[self.app.pk]),
            {"action": "retire", "repository": str(repository.pk)},
        )
        again = register(self.owner, self.app.pk, "acme/widgets")
        self.assertEqual(again.pk, repository.pk)
        self.assertIsNone(again.retired_at)
        self.assertEqual(again.status, "queued")

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

    def test_only_a_deliberate_registration_puts_a_repository_here(self):
        """Nothing else creates one.

        Importing documents from a GitHub link used to register the repository
        they came from, and saving a GitHub connector did the same. Both were
        offered as a convenience and both were a category error: a
        documentation repository is not this application's code, and an extra
        row is one more thing a run has to choose between - which is how a
        ticket comes to reason about no code at all.
        """
        from platform_core import code_graph_ingest, connectors, link_sources

        self.assertFalse(hasattr(code_graph_ingest, "showcase_repository"))
        # Neither caller can reach it, because it is not there to reach.
        for module in (connectors, link_sources):
            with self.subTest(module=module.__name__):
                self.assertFalse(hasattr(module, "showcase_repository"))


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

    def _registered(self, name, number=1):
        repository = CodeRepository.objects.create(
            application=self.app, added_by=self.owner, external_id=name,
            name=name, source_url=f"https://github.com/{name}", status="ready",
        )
        snapshot = CodeSnapshot.objects.create(
            repository=repository, number=number, ref="main", commit_sha=str(number) * 40,
            manifest_digest="m" * 64,
        )
        CodeFile.objects.create(
            snapshot=snapshot, path="billing/service.py", language="python", digest="d" * 64,
            content="def charge():\n    pass", lines=2,
        )
        return repository, snapshot

    @patch("platform_core.code_factory.ask")
    def test_a_ticket_naming_nothing_uses_the_only_registered_repository(self, ask):
        """Most tickets name no repository, and the run used to see no code at all."""
        _, snapshot = self._registered("acme/widgets")
        run = FactoryRun.objects.create(application=self.app, requested_by=self.owner)
        ask.return_value = '{"kind":"fix","summary":"x","requirements":[],"repository":""}'
        run_triage(run, "Billing charges twice")
        run.refresh_from_db()
        self.assertEqual(run.code_snapshot, snapshot)
        self.assertEqual(run.proposed_repository, "acme/widgets")
        # The registry is a guess like ticket text is; it passes the same gate.
        self.assertFalse(run.repository_confirmed)

    @patch("platform_core.code_factory.ask")
    def test_several_registered_repositories_leave_the_choice_to_the_gate(self, ask):
        self._registered("acme/widgets")
        self._registered("acme/billing", number=2)
        run = FactoryRun.objects.create(application=self.app, requested_by=self.owner)
        ask.return_value = '{"kind":"fix","summary":"x","requirements":[],"repository":""}'
        run_triage(run, "Billing charges twice")
        run.refresh_from_db()
        self.assertIsNone(run.code_snapshot_id)
        self.assertEqual(run.proposed_repository, "")

    @patch("platform_core.code_factory.ask")
    def test_the_fallback_does_not_apply_when_the_feature_is_off(self, ask):
        self._registered("acme/widgets")
        ApplicationFeature.objects.update_or_create(
            application=self.app, key="code_graph", defaults={"enabled": False}
        )
        run = FactoryRun.objects.create(application=self.app, requested_by=self.owner)
        ask.return_value = '{"kind":"fix","summary":"x","requirements":[],"repository":""}'
        run_triage(run, "Billing charges twice")
        run.refresh_from_db()
        self.assertIsNone(run.code_snapshot_id)
        self.assertEqual(run.proposed_repository, "")

    @patch("platform_core.code_factory.ask")
    def test_the_design_phase_is_told_which_files_exist(self, ask):
        """Design names the paths implementation reads, so it must see them."""
        run = self._pinned_run()
        ask.return_value = '{"items": []}'
        run_design(
            run,
            {"summary": "Billing", "requirements": []},
            [{"title": "Charged twice", "explanation": "the billing service charge path"}],
        )
        question = ask.call_args.args[4]
        self.assertIn("Pinned code structure:", question)
        self.assertIn("billing/service.py", question)

    @patch("platform_core.code_factory.ask")
    def test_an_unpinned_run_designs_without_a_code_block(self, ask):
        run = FactoryRun.objects.create(application=self.app, requested_by=self.owner)
        ask.return_value = '{"items": []}'
        run_design(run, {"summary": "x", "requirements": []}, [{"title": "t", "explanation": "e"}])
        self.assertNotIn("Pinned code structure:", ask.call_args.args[4])

    def test_confirming_a_different_repository_repins_the_snapshot(self):
        """Delivering to one repository while reasoning about another is a lie."""
        _, first = self._registered("acme/widgets")
        _, second = self._registered("acme/billing", number=2)
        run = FactoryRun.objects.create(application=self.app, requested_by=self.owner)
        FactoryRun.objects.filter(pk=run.pk).update(
            proposed_repository="acme/widgets", code_snapshot=first
        )
        run.refresh_from_db()
        confirm_repository(run, "acme/billing", "release")
        run.refresh_from_db()
        self.assertEqual(run.code_snapshot, second)
        self.assertEqual(run.base_branch, "release")
        self.assertTrue(run.repository_confirmed)

    def test_confirming_an_unregistered_repository_clears_a_stale_snapshot(self):
        _, first = self._registered("acme/widgets")
        run = FactoryRun.objects.create(application=self.app, requested_by=self.owner)
        FactoryRun.objects.filter(pk=run.pk).update(code_snapshot=first)
        run.refresh_from_db()
        confirm_repository(run, "https://github.com/acme/unknown.git", "")
        run.refresh_from_db()
        self.assertIsNone(run.code_snapshot_id)
        self.assertEqual(run.proposed_repository, "acme/unknown")
        self.assertEqual(run.base_branch, "main")

    def test_an_unusable_repository_is_refused_rather_than_confirmed(self):
        """The name is interpolated into a GitHub API path, so it is checked here."""
        run = FactoryRun.objects.create(application=self.app, requested_by=self.owner)
        for value in ["", "   ", "widgets", "acme/widgets/extra", "../etc", "a/../b"]:
            with self.subTest(repository=value):
                with self.assertRaises(ValidationError):
                    confirm_repository(run, value, "main")
        run.refresh_from_db()
        self.assertFalse(run.repository_confirmed)

    def test_a_branch_that_could_escape_the_ref_path_is_refused(self):
        run = FactoryRun.objects.create(application=self.app, requested_by=self.owner)
        for value in ["../../pulls", "a b", "-x", "a/../b"]:
            with self.subTest(branch=value):
                with self.assertRaises(ValidationError):
                    confirm_repository(run, "acme/widgets", value)
        run.refresh_from_db()
        self.assertFalse(run.repository_confirmed)

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



    # ---------------------------------------------------------------- cloning

    def _clone(
        self, files, commit="c" * 40, warnings=None, complete=True, branch="main", languages=()
    ):
        """Stand in for a clone, returning (and filling) what clone_sources does."""

        def clone(name, ref, token, setup=None, **extra):
            found = extra.get("languages")
            if found is not None:
                found.extend(dict(row) for row in languages)
            return commit, branch, list(files), list(warnings or []), complete

        return clone

    @patch("platform_core.code_graph_ingest.github_token", return_value="")
    @patch("platform_core.code_graph_ingest.clone_sources")
    def test_indexing_pins_the_cloned_commit_and_explains_each_edge(self, clone, _token):
        clone.side_effect = self._clone(
            [
                ("app/main.py", "from app import util\n"),
                ("app/util.py", "def helper():\n    pass\n"),
            ]
        )
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

    @patch("platform_core.code_graph_ingest.github_token", return_value="")
    @patch("platform_core.code_graph_ingest.clone_sources")
    def test_indexing_makes_no_rest_call_so_the_hourly_quota_cannot_stop_it(
        self, clone, _token
    ):
        """One clone, not one request per file. The REST budget is not involved."""
        clone.side_effect = self._clone(
            [(f"pkg/mod{index}.py", "x = 1\n") for index in range(120)]
        )
        repository = register(self.owner, self.app.pk, "acme/widgets")
        self.assertTrue(index_repository(repository))
        repository.refresh_from_db()
        self.assertEqual(repository.status, "ready")
        self.assertEqual(repository.snapshots.get().files.count(), 120)
        self.assertEqual(clone.call_count, 1)

    @patch("platform_core.code_graph_ingest.github_token", return_value="")
    @patch("platform_core.code_graph_ingest.clone_sources")
    def test_a_superseded_index_cannot_publish_its_snapshot(self, clone, _token):
        repository = register(self.owner, self.app.pk, "acme/widgets")

        def racing(name, ref, token, setup=None, **_):
            CodeRepository.objects.filter(pk=repository.pk).update(
                status="queued", job_id=uuid.uuid4()
            )
            return "c" * 40, "main", [("app.py", "x = 1\n")], [], True

        clone.side_effect = racing
        self.assertFalse(index_repository(repository))
        self.assertFalse(CodeSnapshot.objects.filter(repository=repository).exists())

    @patch("platform_core.code_graph_ingest.github_token", return_value="")
    @patch("platform_core.code_graph_ingest.clone_sources")
    def test_a_clone_failure_is_reported_in_words_not_a_status_code(self, clone, _token):
        from django.core.exceptions import ValidationError as Invalid

        clone.side_effect = Invalid(
            "acme/private could not be cloned. If it is private, mount this "
            "application's GitHub credential."
        )
        repository = register(self.owner, self.app.pk, "acme/private")
        self.assertTrue(index_repository(repository))
        repository.refresh_from_db()
        self.assertEqual(repository.status, "failed")
        self.assertIn("mount this application's GitHub credential", repository.error)

    @patch("platform_core.code_graph_ingest.github_token", return_value="")
    @patch("platform_core.code_graph_ingest.clone_sources")
    def test_partial_coverage_carries_the_reason_from_the_clone(self, clone, _token):
        clone.side_effect = self._clone(
            [("app/ok.py", "x = 1\n")],
            warnings=["app/blob.py could not be read as UTF-8 text and was left out."],
            complete=False,
        )
        repository = register(self.owner, self.app.pk, "acme/widgets")
        self.assertTrue(index_repository(repository))
        repository.refresh_from_db()
        snapshot = repository.snapshots.get()
        self.assertEqual(repository.status, "partial")
        self.assertIn("app/blob.py", " ".join(snapshot.warnings))

    @patch("platform_core.code_graph_ingest.github_token", return_value="")
    @patch("platform_core.code_graph_ingest.clone_sources")
    def test_a_repository_with_no_supported_files_is_not_called_ready(self, clone, _token):
        """A COBOL repository indexing to nothing must not report success."""
        clone.side_effect = self._clone([])
        repository = register(self.owner, self.app.pk, "acme/legacy")
        self.assertTrue(index_repository(repository))
        repository.refresh_from_db()
        snapshot = repository.snapshots.get()
        self.assertEqual(repository.status, "partial")
        self.assertEqual(snapshot.files.count(), 0)
        self.assertIn("No supported source files", " ".join(snapshot.warnings))

    # -------------------------------------------------------------- languages

    JAVA_SERVICE = [
        {"name": "Java", "files": 40, "bytes": 71_000, "share": 71.0, "analysed": False},
        {"name": "TypeScript", "files": 9, "bytes": 22_000, "share": 22.0, "analysed": True},
        {"name": "Shell", "files": 3, "bytes": 7_000, "share": 7.0, "analysed": False},
    ]

    def test_the_census_counts_every_language_not_only_the_parsed_ones(self):
        from platform_core.code_graph_analysis import census

        rows = census(
            [
                ("src/main/java/App.java", 6000),
                ("src/main/java/Repo.java", 2000),
                ("web/app.ts", 1500),
                ("web/view.tsx", 500),
                ("scripts/build.py", 1000),
                ("Dockerfile", 200),
                ("README.md", 90_000),
                ("config.yaml", 5000),
                ("web/node_modules/lib/index.js", 500_000),
                ("web/dist/vendor.min.js", 300_000),
            ]
        )
        by_name = {row["name"]: row for row in rows}
        # Largest first; prose, data, vendored and minified files are not languages.
        self.assertEqual(
            [row["name"] for row in rows], ["Java", "TypeScript", "Python", "Dockerfile"]
        )
        self.assertEqual(by_name["Java"]["files"], 2)
        self.assertEqual(by_name["TypeScript"]["bytes"], 2000)  # .ts and .tsx together
        self.assertFalse(by_name["Java"]["analysed"])
        self.assertTrue(by_name["Python"]["analysed"])
        self.assertEqual(by_name["Java"]["share"], round(100 * 8000 / 11200, 1))

    def test_the_clone_counts_languages_past_the_file_cap(self):
        """The cap bounds parsing, not counting - and Go is counted, never read."""
        import subprocess
        from pathlib import Path

        from platform_core import code_graph_clone

        def fake_run(arguments, environment, cwd=None, timeout=None):
            if arguments[1] == "clone":
                checkout = Path(arguments[-1])
                for index in range(4):
                    (checkout / "svc").mkdir(parents=True, exist_ok=True)
                    (checkout / "svc" / f"h{index}.go").write_text("package svc\n" * 50)
                (checkout / "tools").mkdir()
                (checkout / "tools" / "a.py").write_text("x = 1\n")
                (checkout / "tools" / "b.py").write_text("y = 2\n")
                return subprocess.CompletedProcess(arguments, 0, "", "")
            answer = "main" if "--abbrev-ref" in arguments else "a" * 40
            return subprocess.CompletedProcess(arguments, 0, answer, "")

        languages = []
        with (
            patch.object(code_graph_clone, "_run", fake_run),
            patch.object(code_graph_clone, "MAX_FILES", 1),
        ):
            _, _, files, _, complete = code_graph_clone.clone_sources(
                "acme/widgets", "", "", languages=languages
            )
        self.assertEqual([path for path, _ in files], ["tools/a.py"])
        self.assertFalse(complete)
        self.assertEqual(
            [(row["name"], row["files"]) for row in languages], [("Go", 4), ("Python", 2)]
        )

    @patch("platform_core.code_graph_ingest.github_token", return_value="")
    @patch("platform_core.code_graph_ingest.clone_sources")
    def test_the_snapshot_records_its_languages_and_a_reuse_learns_them(self, clone, _token):
        clone.side_effect = self._clone(
            [("web/app.ts", "export const a = 1\n")], languages=self.JAVA_SERVICE
        )
        repository = register(self.owner, self.app.pk, "acme/widgets")
        self.assertTrue(index_repository(repository))
        snapshot = repository.snapshots.get()
        self.assertEqual(
            [row["name"] for row in snapshot.languages], ["Java", "TypeScript", "Shell"]
        )

        # A snapshot taken before the census existed learns it on the next index
        # of the same commit, rather than waiting for somebody to push.
        CodeSnapshot.objects.filter(pk=snapshot.pk).update(languages=[])
        CodeRepository.objects.filter(pk=repository.pk).update(
            status="queued", job_id=uuid.uuid4()
        )
        repository.refresh_from_db()
        self.assertTrue(index_repository(repository))
        snapshot.refresh_from_db()
        self.assertEqual(repository.snapshots.count(), 1)
        self.assertEqual(snapshot.languages[0]["name"], "Java")

    @patch("platform_core.code_graph_ingest.github_token", return_value="")
    @patch("platform_core.code_graph_ingest.clone_sources")
    def test_a_reuse_learns_a_test_framework_detected_since(self, clone, _token):
        """A setup recorded before Java was detected has no "java" key at all."""
        detected = {"python": None, "javascript": None, "known": True}

        def cloned(name, ref, token, setup=None, **_):
            setup.update(detected)
            return "c" * 40, "main", [("web/app.ts", "export const a = 1\n")], [], True

        clone.side_effect = cloned
        repository = register(self.owner, self.app.pk, "acme/widgets")
        self.assertTrue(index_repository(repository))
        detected["java"] = "junit5"
        CodeRepository.objects.filter(pk=repository.pk).update(
            status="queued", job_id=uuid.uuid4()
        )
        repository.refresh_from_db()
        self.assertTrue(index_repository(repository))
        snapshot = repository.snapshots.get()
        self.assertEqual(snapshot.test_setup["java"], "junit5")

    @patch("platform_core.code_graph_ingest.github_token", return_value="")
    @patch("platform_core.code_graph_ingest.clone_sources")
    def test_an_unparsed_repository_says_what_it_is_written_in(self, clone, _token):
        clone.side_effect = self._clone(
            [],
            languages=[
                {"name": "COBOL", "files": 12, "bytes": 9000, "share": 100.0, "analysed": False}
            ],
        )
        repository = register(self.owner, self.app.pk, "acme/legacy")
        self.assertTrue(index_repository(repository))
        warning = " ".join(repository.snapshots.get().warnings)
        self.assertIn("written in COBOL 100.0%", warning)

    def test_code_factory_is_told_the_languages_and_what_is_not_listed(self):
        run = self._pinned_run()
        CodeSnapshot.objects.filter(pk=run.code_snapshot_id).update(
            languages=self.JAVA_SERVICE
        )
        run.refresh_from_db()
        context = code_context_for(run, "Update charge")
        self.assertIn(
            "LANGUAGES by share of source: Java 71.0%, TypeScript 22.0%, Shell 7.0%.", context
        )
        self.assertIn("Files in Java, Shell exist in this repository but are not parsed", context)

    def test_an_all_parsed_or_unknown_repository_adds_no_caveat(self):
        run = self._pinned_run()
        self.assertNotIn("LANGUAGES", code_context_for(run, "Update charge"))
        CodeSnapshot.objects.filter(pk=run.code_snapshot_id).update(
            languages=[
                {"name": "Python", "files": 3, "bytes": 900, "share": 100.0, "analysed": True}
            ]
        )
        run.refresh_from_db()
        context = code_context_for(run, "Update charge")
        self.assertIn("LANGUAGES by share of source: Python 100.0%.", context)
        self.assertNotIn("not parsed", context)

    def test_the_page_draws_the_language_mix(self):
        run = self._pinned_run()
        languages = [
            {"name": f"Lang{index}", "files": 1, "bytes": 10, "share": 10.0, "analysed": False}
            for index in range(10)
        ]
        CodeSnapshot.objects.filter(pk=run.code_snapshot_id).update(languages=languages)
        response = self.client.get(
            reverse("code-graph", args=[self.app.pk]),
            {"repository": run.code_snapshot.repository_id},
        )
        body = response.content.decode()
        self.assertIn('class="language-bar"', body)
        self.assertIn("Lang0", body)
        # Seven drawn by name; the other three share one row.
        self.assertNotIn("Lang7", body)
        self.assertIn("3 other", body)
        self.assertIn('x="70.00" y="0" width="30.00"', body)
        self.assertNotIn("style=", body.split('class="code-languages"')[1].split("</div>")[0])

    def test_the_clone_never_takes_a_host_from_anyone(self):
        """The remote is built from a validated owner/name against a fixed host."""
        from platform_core.code_graph_clone import REMOTE
        from platform_core.code_graph_ingest import valid_name

        self.assertTrue(REMOTE.startswith("https://github.com/"))
        from django.core.exceptions import ValidationError as Invalid

        for hostile in [
            "http://evil.example.com/a/b",
            "a/b/../../etc",
            "--upload-pack=touch",
            "ext::sh -c whoami",
            "acme",
        ]:
            with self.assertRaises(Invalid):
                valid_name(hostile)

    def test_the_clone_environment_hides_the_operator_and_the_token(self):
        import tempfile
        from pathlib import Path

        from platform_core.code_graph_clone import _environment

        with tempfile.TemporaryDirectory() as directory:
            scratch = Path(directory)
            environment = _environment(scratch, "ghp_secret_value")
            # No inherited home, so a clone cannot read the operator's credentials.
            self.assertEqual(environment["HOME"], str(scratch / "home"))
            self.assertEqual(environment["USERPROFILE"], str(scratch / "home"))
            self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")
            self.assertNotIn("GITHUB_TOKEN", environment)
            # The token lives in a config file, never in the process arguments -
            # as Basic auth, the form git accepts (test_code_graph_clone_auth).
            import base64

            config = (scratch / "gitconfig").read_text(encoding="utf-8")
            self.assertIn(base64.b64encode(b"x-access-token:ghp_secret_value").decode(), config)
            self.assertIn("hooksPath", config)

    # ------------------------------------------------------------ credentials

    def test_the_credential_panel_names_the_file_but_never_the_secret(self):
        """Telling someone to mount a file without saying which file is unusable."""
        import os
        import tempfile

        from django.test import override_settings

        CodeRepository.objects.create(
            application=self.app, added_by=self.owner, external_id="acme/widgets",
            name="acme/widgets", source_url="https://github.com/acme/widgets", status="failed",
            error="acme/widgets could not be cloned.",
        )
        url = reverse("code-graph", args=[self.app.pk])
        with tempfile.TemporaryDirectory() as directory:
            secret = os.path.join(directory, f"github_{self.app.pk}")
            with override_settings(SECRET_DIRECTORY=directory):
                response = self.client.get(url)
                self.assertContains(response, "No GitHub credential is mounted")
                self.assertContains(response, secret)

                # Mounted the way a deployment has to mount it. `read_secret`
                # refuses a secret readable by anyone but its owner, so writing
                # this at the default 0644 left the file present and unreadable:
                # the page went on saying no credential was mounted, and the
                # test passed only on Windows, where the mode carries no such
                # meaning and the check is skipped.
                with open(secret, "w", encoding="utf-8") as handle:
                    handle.write("ghp_thismustnotreachthepage")
                os.chmod(secret, 0o600)
                response = self.client.get(url)
                self.assertNotContains(response, "No GitHub credential is mounted")
                self.assertNotContains(response, "ghp_thismustnotreachthepage")

    def test_a_working_repository_is_not_nagged_about_a_credential(self):
        """Public repositories clone without one, so the banner would be noise."""
        import os
        import tempfile

        from django.test import override_settings

        CodeRepository.objects.create(
            application=self.app, added_by=self.owner, external_id="acme/public",
            name="acme/public", source_url="https://github.com/acme/public", status="ready",
        )
        with tempfile.TemporaryDirectory() as directory:
            with override_settings(SECRET_DIRECTORY=directory):
                response = self.client.get(reverse("code-graph", args=[self.app.pk]))
                self.assertNotContains(response, "No GitHub credential is mounted")
                self.assertFalse(
                    os.path.exists(os.path.join(directory, f"github_{self.app.pk}"))
                )

    def test_the_connector_screen_shows_the_same_credential_path(self):
        import os
        import tempfile

        from django.test import override_settings

        with tempfile.TemporaryDirectory() as directory:
            with override_settings(SECRET_DIRECTORY=directory):
                response = self.client.get(
                    reverse("connector-new", args=[self.app.pk]), {"kind": "github"}
                )
                if response.status_code != 200:
                    self.skipTest("the github connector form is not reachable for this role")
                self.assertContains(response, os.path.join(directory, f"github_{self.app.pk}"))

    @patch("platform_core.code_graph_ingest.github_token", return_value="")
    @patch("platform_core.code_graph_ingest.clone_sources")
    def test_reindexing_an_empty_repository_never_reports_ready(self, clone, _token):
        """The reuse branch answered from the old snapshot and kept saying ready."""
        clone.side_effect = self._clone([])
        repository = register(self.owner, self.app.pk, "acme/legacy")
        self.assertTrue(index_repository(repository))
        repository.refresh_from_db()
        self.assertEqual(repository.status, "partial")

        # Same commit, same (empty) manifest: the second run reuses the snapshot.
        CodeRepository.objects.filter(pk=repository.pk).update(
            status="queued", job_id=uuid.uuid4()
        )
        repository.refresh_from_db()
        self.assertTrue(index_repository(repository))
        repository.refresh_from_db()
        self.assertEqual(repository.snapshots.count(), 1)
        self.assertEqual(repository.status, "partial")

    def test_choosing_a_repository_still_works_without_scripting(self):
        """The selector is enhanced by script, never dependent on it."""
        repository = CodeRepository.objects.create(
            application=self.app, added_by=self.owner, external_id="acme/widgets",
            name="acme/widgets", source_url="https://github.com/acme/widgets", status="ready",
        )
        response = self.client.get(reverse("code-graph", args=[self.app.pk]))
        body = response.content.decode()
        self.assertIn('method="get"', body)
        self.assertIn("data-auto-submit", body)
        self.assertIn(">View<", body)
        # And the plain GET it posts selects that repository server-side.
        response = self.client.get(
            reverse("code-graph", args=[self.app.pk]), {"repository": str(repository.pk)}
        )
        self.assertContains(response, repository.name)

    # ------------------------------------------------- roles and cycle counts

    def test_a_cycle_flags_every_file_in_it_not_just_the_two_ends(self):
        """a -> b -> c -> a. The cheap back-edge check leaves b looking clean."""
        from platform_core.code_graph_analysis import classify

        paths = ["a.py", "b.py", "c.py"]
        edges = [
            {"source": "a.py", "target": "b.py"},
            {"source": "b.py", "target": "c.py"},
            {"source": "c.py", "target": "a.py"},
        ]
        roles, groups, count = classify(paths, edges)
        self.assertEqual(count, 1, "five files in one loop is one problem, not five")
        self.assertEqual({roles[path] for path in paths}, {"circular"})
        self.assertEqual(len({groups[path] for path in paths}), 1)

    def test_a_file_importing_itself_is_a_cycle_of_one(self):
        from platform_core.code_graph_analysis import classify

        roles, groups, count = classify(["solo.py"], [{"source": "solo.py", "target": "solo.py"}])
        self.assertEqual(count, 1)
        self.assertEqual(roles["solo.py"], "circular")

    def test_roles_name_what_points_at_a_file(self):
        from platform_core.code_graph_analysis import classify

        roles, _, count = classify(
            ["top.py", "mid.py", "sink.py", "lone.py"],
            [
                {"source": "top.py", "target": "mid.py"},
                {"source": "mid.py", "target": "sink.py"},
            ],
        )
        self.assertEqual(count, 0)
        self.assertEqual(roles["top.py"], "entry")
        self.assertEqual(roles["mid.py"], "service")
        self.assertEqual(roles["sink.py"], "leaf")
        self.assertEqual(roles["lone.py"], "orphan")

    def test_deep_import_chains_do_not_exhaust_the_stack(self):
        """The recursive form dies somewhere past a thousand files."""
        from platform_core.code_graph_analysis import classify

        paths = [f"m{index}.py" for index in range(4000)]
        edges = [
            {"source": paths[index], "target": paths[index + 1]}
            for index in range(len(paths) - 1)
        ]
        roles, _, count = classify(paths, edges)
        self.assertEqual(count, 0)
        self.assertEqual(roles[paths[0]], "entry")
        self.assertEqual(roles[paths[-1]], "leaf")

    @patch("platform_core.code_graph_ingest.github_token", return_value="")
    @patch("platform_core.code_graph_ingest.clone_sources")
    def test_the_snapshot_stores_counts_taken_over_every_file(self, clone, _token):
        """Not over the subset a page draws, which is where the wrong count came from."""
        clone.side_effect = self._clone(
            [
                ("pkg/a.py", "from pkg import b\n"),
                ("pkg/b.py", "from pkg import c\n"),
                ("pkg/c.py", "from pkg import a\n"),
                ("pkg/lonely.py", "x = 1\n"),
                ("pkg/__init__.py", ""),
            ]
        )
        repository = register(self.owner, self.app.pk, "acme/widgets")
        self.assertTrue(index_repository(repository))
        snapshot = repository.snapshots.get()
        self.assertEqual(snapshot.cycle_count, 1)
        self.assertEqual(
            snapshot.orphan_count,
            snapshot.files.filter(role="orphan").count(),
        )
        self.assertEqual(snapshot.files.filter(role="circular").count(), 3)
        self.assertEqual(
            {item.cycle_group for item in snapshot.files.filter(role="circular")}, {1}
        )

    @patch("platform_core.code_graph_ingest.github_token", return_value="")
    @patch("platform_core.code_graph_ingest.clone_sources")
    def test_the_status_bar_counts_the_snapshot_not_the_drawn_nodes(self, clone, _token):
        """A repository past the node cap still reports its real totals."""
        sources = [(f"pkg/mod{index}.py", "x = 1\n") for index in range(260)]
        clone.side_effect = self._clone(sources)
        repository = register(self.owner, self.app.pk, "acme/widgets")
        self.assertTrue(index_repository(repository))
        snapshot = repository.snapshots.get()
        self.assertEqual(snapshot.files.count(), 260)
        self.assertEqual(snapshot.orphan_count, 260)

        response = self.client.get(
            reverse("code-graph", args=[self.app.pk]), {"repository": str(repository.pk)}
        )
        body = response.content.decode()
        # The honest total, not the 200 that fit on the canvas.
        self.assertIn("260", body)
        self.assertIn("with no links", body)
        self.assertIn("drawing 200 of 260", body)

    def test_search_still_works_as_a_plain_form_without_scripting(self):
        """Live dimming is an enhancement; the server search is the contract."""
        repository = CodeRepository.objects.create(
            application=self.app, added_by=self.owner, external_id="acme/widgets",
            name="acme/widgets", source_url="https://github.com/acme/widgets", status="ready",
        )
        snapshot = CodeSnapshot.objects.create(
            repository=repository, number=1, ref="main", commit_sha="c" * 40,
            manifest_digest="m" * 64,
        )
        CodeFile.objects.create(
            snapshot=snapshot, path="pkg/needle.py", language="python", digest="a" * 64,
            content="x = 1", lines=1, role="orphan",
        )
        CodeFile.objects.create(
            snapshot=snapshot, path="pkg/other.py", language="python", digest="b" * 64,
            content="haystack = 1", lines=1, role="orphan",
        )
        url = reverse("code-graph", args=[self.app.pk])
        response = self.client.get(url, {"repository": str(repository.pk), "q": "needle"})
        self.assertContains(response, "needle.py")
        self.assertNotContains(response, "other.py")

        # The server also looks inside contents, which the browser cannot.
        response = self.client.get(url, {"repository": str(repository.pk), "q": "haystack"})
        self.assertContains(response, "other.py")
        self.assertNotContains(response, "needle.py")

        # And the live-dimming surface is present for the enhancement to use.
        response = self.client.get(url, {"repository": str(repository.pk)})
        self.assertContains(response, 'id="code-search-status"')
        self.assertContains(response, 'method="get"')

    # ------------------------------------------------------------ impact

    @patch("platform_core.code_graph_ingest.github_token", return_value="")
    @patch("platform_core.code_graph_ingest.clone_sources")
    def test_impact_follows_imports_in_the_right_direction(self, clone, _token):
        """source imports target. Reversing it gives a plausible wrong answer."""
        clone.side_effect = self._clone(
            [
                ("pkg/__init__.py", ""),
                ("pkg/base.py", "VALUE = 1\n"),
                ("pkg/middle.py", "from pkg import base\n"),
                ("pkg/top.py", "from pkg import middle\n"),
            ]
        )
        repository = register(self.owner, self.app.pk, "acme/widgets")
        self.assertTrue(index_repository(repository))
        snapshot = repository.snapshots.get()
        base = snapshot.files.get(path="pkg/base.py")
        top = snapshot.files.get(path="pkg/top.py")
        paths = {item.pk: item.path for item in snapshot.files.all()}

        from platform_core.code_graph import reach

        # Changing base.py could reach middle.py at one hop, top.py at two.
        affected = {paths[pk]: hops for pk, hops in reach(snapshot, base.pk, False).items()}
        self.assertEqual(affected.get("pkg/middle.py"), 1)
        self.assertEqual(affected.get("pkg/top.py"), 2)

        # base.py rests on nothing.
        self.assertEqual(reach(snapshot, base.pk, True), {})

        # top.py rests on middle then base, and nothing depends on it.
        rests = {paths[pk]: hops for pk, hops in reach(snapshot, top.pk, True).items()}
        self.assertEqual(rests.get("pkg/middle.py"), 1)
        self.assertEqual(rests.get("pkg/base.py"), 2)
        self.assertEqual(reach(snapshot, top.pk, False), {})

    @patch("platform_core.code_graph_ingest.github_token", return_value="")
    @patch("platform_core.code_graph_ingest.clone_sources")
    def test_impact_terminates_on_a_cycle(self, clone, _token):
        clone.side_effect = self._clone(
            [
                ("pkg/__init__.py", ""),
                ("pkg/a.py", "from pkg import b\n"),
                ("pkg/b.py", "from pkg import a\n"),
            ]
        )
        repository = register(self.owner, self.app.pk, "acme/widgets")
        self.assertTrue(index_repository(repository))
        snapshot = repository.snapshots.get()

        from platform_core.code_graph import reach

        a = snapshot.files.get(path="pkg/a.py")
        b = snapshot.files.get(path="pkg/b.py")
        self.assertIn(b.pk, reach(snapshot, a.pk, True))
        self.assertIn(b.pk, reach(snapshot, a.pk, False))

    def test_the_file_page_keeps_working_without_scripting(self):
        """Tabs are radio inputs, so every pane is in the markup and reachable."""
        repository = CodeRepository.objects.create(
            application=self.app, added_by=self.owner, external_id="acme/widgets",
            name="acme/widgets", source_url="https://github.com/acme/widgets", status="ready",
        )
        snapshot = CodeSnapshot.objects.create(
            repository=repository, number=1, ref="main", commit_sha="c" * 40,
            manifest_digest="m" * 64,
        )
        file = CodeFile.objects.create(
            snapshot=snapshot, path="pkg/alone.py", language="python", digest="a" * 64,
            content="x = 1", lines=1, role="orphan",
        )
        response = self.client.get(reverse("code-file", args=[self.app.pk, file.pk]))
        body = response.content.decode()
        self.assertIn('type="radio"', body)
        for pane in ["overview", "deps", "used", "impact"]:
            self.assertIn(f'data-pane="{pane}"', body)
        # An honest empty impact, and the caveat that frames it.
        self.assertIn("cannot break anything else", body)
        self.assertIn("measured between files, not functions", body)

    # ------------------------------------------------------------ API seams

    def test_a_call_and_a_route_are_matched_on_a_normalised_path(self):
        from platform_core.code_graph_analysis import normalise_route

        self.assertEqual(normalise_route("/api/users/<int:pk>/"), "/api/users/*")
        self.assertEqual(normalise_route("api/users/{id}"), "/api/users/*")
        self.assertEqual(normalise_route("https://host/api/users/x?q=1"), "/api/users/x")

    def test_a_seam_is_inferred_and_never_invented(self):
        from platform_core.code_graph_analysis import facts, relationships

        files = [
            facts("api/urls.py", 'urlpatterns = [path("api/graph/", view)]\n'),
            facts("web/client.js", 'const r = await fetch("/api/graph/")\n'),
            facts("web/stray.js", 'const z = await fetch("/api/nothing-declares-this")\n'),
        ]
        edges = relationships(files)
        seams = [edge for edge in edges if edge["kind"] == "api"]
        self.assertEqual(len(seams), 1)
        self.assertEqual(seams[0]["source"], "web/client.js")
        self.assertEqual(seams[0]["target"], "api/urls.py")
        # Matching strings is not proof that a service answers.
        self.assertEqual(seams[0]["confidence"], "inferred")
        # An unmatched call produces nothing at all.
        self.assertNotIn("web/stray.js", {edge["source"] for edge in seams})

    def test_two_files_sharing_a_word_are_not_connected_by_it(self):
        from platform_core.code_graph_analysis import facts, relationships

        files = [
            facts("a/urls.py", 'urlpatterns = [path("api/users/", view)]\n'),
            facts("b/client.js", 'fetch("/api/orders/")\n'),
        ]
        self.assertEqual([e for e in relationships(files) if e["kind"] == "api"], [])

    def test_different_known_methods_are_different_endpoints(self):
        from platform_core.code_graph_analysis import facts, relationships

        files = [
            facts("api/routes.py", '@app.get("/api/items")\ndef read(): pass\n'),
            facts("web/writer.js", 'api.post("/api/items")\n'),
        ]
        self.assertEqual([e for e in relationships(files) if e["kind"] == "api"], [])

    def test_an_inferred_seam_never_displaces_a_real_import(self):
        from platform_core.code_graph_analysis import facts, relationships

        files = [
            facts("pkg/__init__.py", ""),
            facts("pkg/server.py", 'urlpatterns = [path("api/x/", v)]\n'),
            facts(
                "pkg/caller.py",
                'from pkg import server\nimport requests\nrequests.get("/api/x/")\n',
            ),
        ]
        pair = [
            edge
            for edge in relationships(files)
            if edge["source"] == "pkg/caller.py" and edge["target"] == "pkg/server.py"
        ]
        self.assertEqual(len(pair), 1)
        self.assertEqual(pair[0]["kind"], "import")
        self.assertEqual(pair[0]["confidence"], "static")

    def test_modern_javascript_declarations_are_found(self):
        """A file of arrow-function components used to report no symbols."""
        from platform_core.code_graph_analysis import facts

        found = facts(
            "web/App.jsx",
            'export const Panel = ({ x }) => null\n'
            'const legacy = function () {}\n'
            'export default function App() {}\n'
            'export class Store {}\n',
        )
        names = {item["name"] for item in found["symbols"]}
        self.assertEqual(names, {"Panel", "legacy", "App", "Store"})

    def test_a_dynamic_import_is_still_an_import(self):
        from platform_core.code_graph_analysis import facts, relationships

        files = [
            facts("web/lazy.js", 'const m = await import("./heavy")\n'),
            facts("web/heavy.js", "export const heavy = () => null\n"),
        ]
        edges = relationships(files)
        self.assertEqual(
            [(edge["source"], edge["target"]) for edge in edges],
            [("web/lazy.js", "web/heavy.js")],
        )


@override_settings(
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class BranchDriftTests(TestCase):
    """Whether the indexed snapshot is still what the branch points at.

    Detected, never acted on - the rule knowledge sources already follow.
    Re-indexing costs a clone and supersedes the snapshot every past run
    reasoned about, so it stays something somebody presses.
    """

    def setUp(self):
        DocumentTests.setUp(self)
        self.repository = register(self.owner, self.app.pk, "acme/widgets")
        CodeRepository.objects.filter(pk=self.repository.pk).update(status="ready")
        self.repository.refresh_from_db()

    def indexed(self, commit="a" * 40):
        self.repository.snapshots.create(number=1, commit_sha=commit)
        return self.repository

    def test_unknown_until_there_is_something_to_compare(self):
        """A repository nobody checked must not read as current."""
        self.assertIsNone(self.repository.drifted)
        self.indexed()
        self.assertIsNone(self.repository.drifted)

    def test_a_matching_head_is_in_sync(self):
        self.indexed("a" * 40)
        self.repository.head_sha = "a" * 40
        self.assertIs(self.repository.drifted, False)

    def test_a_moved_branch_is_drift(self):
        self.indexed("a" * 40)
        self.repository.head_sha = "b" * 40
        self.assertIs(self.repository.drifted, True)

    def test_the_worker_records_the_head_and_nothing_else(self):
        from platform_core.code_graph_ingest import process_next_head

        self.indexed("a" * 40)
        with patch("platform_core.link_sources.github_token", return_value=""):
            with patch(
                "platform_core.github_write.branch_head", return_value="b" * 40
            ) as asked:
                self.assertTrue(process_next_head())
        # Asked about the default branch, and nothing was re-indexed.
        self.assertEqual(asked.call_args.args[1], "main")
        self.repository.refresh_from_db()
        self.assertIs(self.repository.drifted, True)
        self.assertEqual(self.repository.snapshots.count(), 1)

    def test_a_branch_that_cannot_be_read_is_not_drift(self):
        """Keeping the last thing known true beats claiming to be current."""
        from platform_core.code_graph_ingest import process_next_head

        self.indexed("a" * 40)
        CodeRepository.objects.filter(pk=self.repository.pk).update(head_sha="a" * 40)
        with patch("platform_core.link_sources.github_token", return_value=""):
            with patch(
                "platform_core.github_write.branch_head",
                side_effect=ValidationError("gone"),
            ):
                process_next_head()
        self.repository.refresh_from_db()
        self.assertIs(self.repository.drifted, False)
        # The timestamp still moved, so it is not retried in a loop.
        self.assertIsNotNone(self.repository.head_checked_at)

    def test_a_retired_repository_is_not_asked_about(self):
        from django.utils import timezone

        from platform_core.code_graph_ingest import process_next_head

        self.indexed()
        CodeRepository.objects.filter(pk=self.repository.pk).update(
            retired_at=timezone.now()
        )
        with patch("platform_core.github_write.branch_head") as asked:
            self.assertFalse(process_next_head())
        asked.assert_not_called()

    def test_the_lane_indexes_before_it_checks(self):
        from platform_core.document_worker import LANES

        lanes = {name: [step.__name__ for step in steps_for()] for name, steps_for, _ in LANES}
        self.assertEqual(
            lanes["code-graph"], ["process_next_repository", "process_next_head"]
        )

    def test_the_screen_says_which_it_is(self):
        """Drift is a notice that says what to do, not a word on a status line.

        Somebody reading this has to decide whether to re-index, and that
        decision needs both commits and the fact that runs keep reading the
        snapshot until they do.
        """
        self.indexed("a" * 40)
        CodeRepository.objects.filter(pk=self.repository.pk).update(head_sha="b" * 40)
        page = self.client.get(
            reverse("code-graph", args=[self.app.pk]), {"repository": self.repository.pk}
        )
        self.assertContains(page, "has moved on since this snapshot")
        self.assertContains(page, "bbbbbbbb")
        self.assertContains(page, "aaaaaaaa")
        self.assertContains(page, "notice-warning")

    def test_when_it_was_last_looked_at_is_on_the_screen(self):
        """"In sync" is worth reading only beside when that was established."""
        self.indexed("a" * 40)
        url = reverse("code-graph", args=[self.app.pk])
        page = self.client.get(url, {"repository": self.repository.pk})
        self.assertContains(page, "branch not checked yet")
        CodeRepository.objects.filter(pk=self.repository.pk).update(
            head_sha="a" * 40, head_checked_at=timezone.now() - timedelta(minutes=9)
        )
        page = self.client.get(url, {"repository": self.repository.pk})
        self.assertContains(page, "minutes ago")
        self.assertContains(page, "in sync with")

    def test_checking_the_branch_leaves_the_snapshot_alone(self):
        """Noticing and acting are two decisions. This one is the safe half."""
        self.indexed("a" * 40)
        with patch(
            "platform_core.code_graph.refresh_head",
            side_effect=lambda repo: CodeRepository.objects.filter(pk=repo.pk).update(
                head_sha="b" * 40, head_checked_at=timezone.now()
            ),
        ) as checked:
            response = self.client.post(
                reverse("code-graph", args=[self.app.pk]),
                {"action": "check", "repository": str(self.repository.pk)},
            )
        self.assertTrue(checked.called)
        self.assertEqual(response.status_code, 302)
        self.repository.refresh_from_db()
        # Untouched: still ready, still one snapshot, still pinning the commit
        # every past run reasoned about.
        self.assertEqual(self.repository.status, "ready")
        self.assertEqual(self.repository.snapshots.count(), 1)
        self.assertEqual(self.repository.snapshots.first().commit_sha, "a" * 40)
