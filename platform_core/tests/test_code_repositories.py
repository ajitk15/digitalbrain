"""Knowledge → Code Repos: the one list of repositories Code Graph reads.

Code Graph used to be its own menu item that also added, checked, re-indexed
and removed repositories. Those now live on Code Repos, under Knowledge, and
Code Graph's Structure, Reviews and Documentation read from that list. These pin
the move: who may see and change the list, that every action keeps its rules,
that each half of Knowledge is gated on its own feature, and that the old POST
to Code Graph still works.
"""

from unittest.mock import patch

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from platform_core.code_graph_ingest import register
from platform_core.models import (
    ApplicationFeature,
    AuditEvent,
    CodeRepository,
    Document,
)

from .test_documents import DocumentTests


@override_settings(
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class CodeRepositoriesTests(TestCase):
    def setUp(self):
        DocumentTests.setUp(self)
        self.url = reverse("code-repositories", args=[self.app.pk])

    def indexed(self, name="acme/widgets"):
        repository = register(self.owner, self.app.pk, name)
        repository.status = "ready"
        repository.save(update_fields=["status"])
        repository.snapshots.create(number=1, commit_sha="a" * 40, languages=[
            {"name": "Python", "files": 3, "share": 100.0, "analysed": True},
        ])
        return repository

    def documents_only(self, name="acme/handbook"):
        return CodeRepository.objects.create(
            application=self.app,
            provider="github",
            external_id=name,
            name=name,
            source_url=f"https://github.com/{name}",
            added_by=self.owner,
            status="documentation",
        )

    # ---- who may see and change it ----

    def test_no_grant_reads_as_not_found(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.client.post(self.url, {"action": "retire"}).status_code, 404)

    def test_a_viewer_sees_the_list_and_no_controls(self):
        repository = self.indexed()
        self.client.force_login(self.viewer)
        page = self.client.get(self.url)
        self.assertContains(page, repository.name)
        self.assertNotContains(page, 'value="retire"')
        self.assertNotContains(page, reverse("code-graph-add", args=[self.app.pk]))
        response = self.client.post(
            self.url, {"action": "retire", "repository": str(repository.pk)}
        )
        self.assertEqual(response.status_code, 403)
        repository.refresh_from_db()
        self.assertIsNone(repository.retired_at)

    def test_another_applications_repository_reads_as_not_found(self):
        foreign = CodeRepository.objects.create(
            application=self.other,
            provider="github",
            external_id="acme/foreign",
            name="acme/foreign",
            source_url="https://github.com/acme/foreign",
            added_by=self.owner,
            status="ready",
        )
        for action in ("retire", "refresh", "check", "index_code"):
            with self.subTest(action=action):
                response = self.client.post(
                    self.url, {"action": action, "repository": str(foreign.pk)}
                )
                self.assertEqual(response.status_code, 404)
        foreign.refresh_from_db()
        self.assertIsNone(foreign.retired_at)
        self.assertEqual(foreign.status, "ready")

    def test_an_unknown_action_is_refused(self):
        self.assertEqual(self.client.post(self.url, {"action": "delete"}).status_code, 400)

    # ---- the list ----

    def test_each_row_says_what_is_indexed_and_opens_its_structure(self):
        repository = self.indexed()
        page = self.client.get(self.url)
        self.assertContains(page, "aaaaaaaa")
        self.assertContains(page, "Python")
        self.assertContains(
            page, f'{reverse("code-graph", args=[self.app.pk])}?repository={repository.pk}'
        )
        self.assertContains(page, "chat and the knowledge graph do not read it")
        self.assertContains(
            page, f'href="{reverse("code-graph-add", args=[self.app.pk])}" data-modal'
        )

    def test_the_list_costs_the_same_however_many_repositories(self):
        self.indexed("acme/one")
        with CaptureQueriesContext(connection) as one:
            self.client.get(self.url)
        for name in ("acme/two", "acme/three", "acme/four"):
            self.indexed(name)
        with CaptureQueriesContext(connection) as four:
            self.client.get(self.url)
        self.assertEqual(
            len(one), len(four), "\n".join(q["sql"][:160] for q in four.captured_queries)
        )

    def test_a_repository_still_indexing_reloads_the_page(self):
        register(self.owner, self.app.pk, "acme/widgets")
        self.assertContains(self.client.get(self.url), "data-document-pending")

    def test_documents_imported_from_a_repository_are_counted_and_linked(self):
        repository = self.indexed()
        Document.objects.create(
            application=self.app,
            uploaded_by=self.owner,
            name="README.md",
            size=1,
            sha256="0" * 64,
            status="ready",
            origin="github",
            source_url="https://raw.githubusercontent.com/Acme/Widgets/main/README.md",
        )
        page = self.client.get(self.url)
        self.assertContains(page, "1 document<")
        self.assertContains(page, f"?q={repository.external_id}")

    # ---- the actions, moved here with their rules ----

    def test_retiring_keeps_the_row_and_says_so(self):
        repository = self.indexed()
        response = self.client.post(
            self.url, {"action": "retire", "repository": str(repository.pk)}, follow=True
        )
        self.assertRedirects(response, self.url)
        self.assertContains(response, "was removed from this application")
        repository.refresh_from_db()
        self.assertIsNotNone(repository.retired_at)
        self.assertTrue(
            AuditEvent.objects.filter(
                action="code_repository.retired", resource_id=str(repository.pk)
            ).exists()
        )
        self.assertNotContains(self.client.get(self.url), 'value="retire"')

    def test_refresh_queues_an_index_and_keeps_the_snapshot(self):
        repository = self.indexed()
        response = self.client.post(
            self.url, {"action": "refresh", "repository": str(repository.pk)}
        )
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        repository.refresh_from_db()
        self.assertEqual(repository.status, "queued")
        self.assertEqual(repository.snapshots.count(), 1)

    def test_check_asks_where_the_branch_points_and_changes_nothing_else(self):
        repository = self.indexed()
        with patch("platform_core.code_graph.refresh_head") as head:
            response = self.client.post(
                self.url, {"action": "check", "repository": str(repository.pk)}
            )
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        head.assert_called_once()
        repository.refresh_from_db()
        self.assertEqual(repository.status, "ready")

    def test_the_same_post_to_code_graph_still_works(self):
        """A page left open from before the move posts to the old address."""
        repository = self.indexed()
        response = self.client.post(
            reverse("code-graph", args=[self.app.pk]),
            {"action": "refresh", "repository": str(repository.pk)},
        )
        self.assertEqual(response.status_code, 302)
        repository.refresh_from_db()
        self.assertEqual(repository.status, "queued")

    def test_adding_a_repository_lands_on_code_repos(self):
        response = self.client.post(
            reverse("code-graph-add", args=[self.app.pk]),
            {"action": "register", "repository": "acme/widgets"},
        )
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.assertTrue(CodeRepository.objects.filter(application=self.app).exists())

    # ---- a repository whose documents were imported but not its code ----

    def test_a_documents_only_repository_offers_to_index_its_code(self):
        repository = self.documents_only()
        page = self.client.get(self.url)
        self.assertContains(page, "Documents only")
        self.assertContains(page, 'value="index_code"')
        self.assertNotContains(
            page, f'{reverse("code-graph", args=[self.app.pk])}?repository={repository.pk}'
        )
        self.client.post(self.url, {"action": "index_code", "repository": str(repository.pk)})
        repository.refresh_from_db()
        self.assertEqual(repository.status, "queued")
        self.assertTrue(
            AuditEvent.objects.filter(
                action="code_repository.queued", resource_id=str(repository.pk)
            ).exists()
        )

    def test_index_code_is_only_for_a_documents_only_repository(self):
        repository = self.indexed()
        response = self.client.post(
            self.url, {"action": "index_code", "repository": str(repository.pk)}
        )
        self.assertEqual(response.status_code, 404)
        repository.refresh_from_db()
        self.assertEqual(repository.status, "ready")

    # ---- each half of Knowledge on its own feature ----

    def test_code_graph_lives_under_knowledge_not_beside_it(self):
        repository = self.indexed()
        for url in (
            self.url,
            reverse("code-graph", args=[self.app.pk]),
            reverse("code-reviews", args=[self.app.pk]),
            reverse("code-docs", args=[self.app.pk]),
            reverse("code-file", args=[self.app.pk, repository.snapshots.first().files.create(
                path="app.py", language="python", digest="d", content="").pk]),
        ):
            with self.subTest(url=url):
                body = self.client.get(url).content.decode()
                self.assertNotIn(">Code Graph</a>", body)
                menu = body.split('aria-label="Application menu"')[1].split("</nav>")[0]
                self.assertIn('aria-current="page"', menu.split("Knowledge</a>")[0])
                tabs = body.split('aria-label="Knowledge views"')[1].split("</nav>")[0]
                current = tabs.split('aria-current="page"')[1]
                self.assertTrue(current.split("</a>")[0].endswith("Code Repos"), current[:300])

    def test_the_structure_tab_is_not_a_second_graph_and_keeps_the_repository(self):
        repository = self.indexed()
        body = self.client.get(
            reverse("code-graph", args=[self.app.pk]), {"repository": repository.pk}
        ).content.decode()
        views = body.split('aria-label="Code Graph views"')[1].split("</nav>")[0]
        self.assertIn("Structure", views)
        self.assertNotIn(">Graph<", views)
        self.assertIn(
            f'{reverse("code-docs", args=[self.app.pk])}?repository={repository.pk}', views
        )

    def test_with_code_graph_off_there_is_no_code_repos(self):
        ApplicationFeature.objects.create(application=self.app, key="code_graph", enabled=False)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        body = self.client.get(reverse("documents", args=[self.app.pk])).content.decode()
        self.assertNotIn(self.url, body)
        self.assertIn("Documents</a>", body)

    def test_with_knowledge_off_code_repos_is_still_reachable(self):
        """Repositories exist for Code Graph and Code Factory; Knowledge being
        off must not take away the only place to add one."""
        ApplicationFeature.objects.create(application=self.app, key="knowledge", enabled=False)
        page = self.client.get(self.url)
        self.assertEqual(page.status_code, 200)
        body = page.content.decode()
        menu = body.split('aria-label="Application menu"')[1].split("</nav>")[0]
        self.assertIn(f'href="{self.url}"', menu)
        tabs = body.split('aria-label="Knowledge views"')[1].split("</nav>")[0]
        self.assertIn("Code Repos", tabs)
        for hidden in ("Documents", "Graph", "Quality", "Versions"):
            self.assertNotIn(f"{hidden}</a>", tabs)

    def test_with_both_off_knowledge_leaves_the_menu(self):
        for key in ("knowledge", "code_graph"):
            ApplicationFeature.objects.create(application=self.app, key=key, enabled=False)
        body = self.client.get(
            reverse("application-features", args=[self.app.pk])
        ).content.decode()
        menu = body.split('aria-label="Application menu"')[1].split("</nav>")[0]
        self.assertNotIn("Knowledge</a>", menu)
