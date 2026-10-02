"""Code Graph reading a repository from a folder on this server.

A browser user naming a server path is the risk, so most of these pin the
boundary: nothing is offered until an operator lists roots, nothing outside them
resolves, a symlink is judged by where it lands, credentials are never walked
into, and every check is repeated when the index actually runs.
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from digitalbrain.configuration import load_config
from platform_core import code_graph_local
from platform_core.code_graph_ingest import index_repository, refresh_head, register_local
from platform_core.models import CodeRepository

from .test_documents import DocumentTests

SHA = "a" * 40
LATER = "b" * 40


def write(path, text=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def git(folder, head="ref: refs/heads/main\n", refs=None, packed=""):
    """A .git directory as files, which is all this ever reads of one."""
    write(folder / ".git/HEAD", head)
    for name, value in (refs or {}).items():
        write(folder / ".git" / name, value + "\n")
    if packed:
        write(folder / ".git/packed-refs", packed)


class LocalRootsConfigurationTests(SimpleTestCase):
    def load(self, roots):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            secrets = Path(directory).as_posix()
            path.write_text(
                f'mode = "development"\nhosts = ["localhost"]\nsecret_directory = "{secrets}"\n'
                f"code_graph_local_roots = {roots}\n",
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"DIGITAL_BRAIN_CONFIG": str(path)}):
                return load_config()

    def test_absolute_folders_are_accepted(self):
        folder = Path(tempfile.gettempdir()).resolve().as_posix()
        self.assertEqual(self.load(f'["{folder}"]')["code_graph_local_roots"], [folder])

    def test_a_drive_root_is_refused_like_a_wildcard_host(self):
        anchor = Path(tempfile.gettempdir()).resolve().anchor.replace("\\", "/")
        with self.assertRaises(ImproperlyConfigured):
            self.load(f'["{anchor}"]')

    def test_a_relative_path_or_a_non_list_is_refused(self):
        for value in ('["code"]', '"C:/Workspace"', "[1]", '[""]'):
            with self.subTest(value=value), self.assertRaises(ImproperlyConfigured):
                self.load(value)


@override_settings(
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class LocalFolderTests(TestCase):
    def setUp(self):
        DocumentTests.setUp(self)
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.base = Path(scratch.name).resolve()
        self.root = self.base / "code"
        self.outside = self.base / "elsewhere"
        self.secrets = self.root / "service" / "secrets"
        self.service = self.root / "service"
        write(self.service / "app/main.py", "from app import util\n")
        write(self.service / "app/util.py", "def helper():\n    pass\n")
        write(self.service / "node_modules/left/index.js", "module.exports = 1\n")
        write(self.service / ".hidden/tool.py", "x = 1\n")
        write(self.secrets / "jira.py", "TOKEN = 'never read'\n")
        write(self.outside / "private.py", "x = 1\n")
        roots = override_settings(
            CODE_GRAPH_LOCAL_ROOTS=[str(self.root)],
            CONNECTOR_SECRET_DIRECTORY=str(self.secrets),
        )
        roots.enable()
        self.addCleanup(roots.disable)

    # ------------------------------------------------------------- boundary

    def test_nothing_is_offered_until_an_operator_lists_roots(self):
        add = reverse("code-graph-add", args=[self.app.pk])
        self.assertContains(self.client.get(add), 'value="register_folder"')
        with override_settings(CODE_GRAPH_LOCAL_ROOTS=[]):
            self.assertNotContains(self.client.get(add), 'value="register_folder"')
            response = self.client.post(
                add, {"action": "register_folder", "folder": str(self.service)}
            )
            self.assertEqual(response.status_code, 403)
            with self.assertRaises(ValidationError):
                code_graph_local.resolve_folder(str(self.service))
        self.assertFalse(CodeRepository.objects.exists())

    def test_a_folder_outside_the_roots_is_refused(self):
        for value in (str(self.outside), str(self.root / ".." / "elsewhere"), "code", ""):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                code_graph_local.resolve_folder(value)

    def test_a_symlink_is_judged_by_where_it_lands(self):
        link = self.root / "shortcut"
        try:
            os.symlink(self.outside, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("This account cannot create symlinks.")
        with self.assertRaises(ValidationError):
            code_graph_local.resolve_folder(str(link))

    def test_a_credentials_folder_cannot_be_registered(self):
        with self.assertRaises(ValidationError):
            code_graph_local.resolve_folder(str(self.secrets))

    def test_viewers_cannot_register_a_folder(self):
        self.client.force_login(self.viewer)
        response = self.client.post(
            reverse("code-graph-add", args=[self.app.pk]),
            {"action": "register_folder", "folder": str(self.service)},
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(CodeRepository.objects.exists())

    def test_a_refusal_is_shown_on_the_form(self):
        response = self.client.post(
            reverse("code-graph-add", args=[self.app.pk]),
            {"action": "register_folder", "folder": str(self.outside)},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "outside the folders this server may read")

    # ------------------------------------------------------------- indexing

    def register(self):
        response = self.client.post(
            reverse("code-graph-add", args=[self.app.pk]),
            {"action": "register_folder", "folder": str(self.service)},
        )
        self.assertEqual(response.status_code, 302)
        return CodeRepository.objects.get()

    def test_a_git_checkout_is_pinned_to_its_commit_without_running_git(self):
        git(self.service, refs={"refs/heads/main": SHA})
        repository = self.register()
        self.assertEqual(repository.provider, "local")
        self.assertEqual(repository.name, "service")
        self.assertEqual(code_graph_local.path_of(repository), self.service)
        with patch("subprocess.run", side_effect=AssertionError("git must not run")):
            self.assertTrue(index_repository(repository))
        repository.refresh_from_db()
        self.assertEqual(repository.status, "ready", repository.error)
        self.assertEqual(repository.default_ref, "main")
        snapshot = repository.snapshots.get()
        self.assertEqual(snapshot.commit_sha, SHA)
        self.assertEqual(
            sorted(snapshot.files.values_list("path", flat=True)),
            ["app/main.py", "app/util.py"],
        )
        self.assertEqual(snapshot.relationships.count(), 1)

    def test_dependency_hidden_and_credential_folders_are_never_read(self):
        repository = self.register()
        self.assertTrue(index_repository(repository))
        paths = set(repository.snapshots.get().files.values_list("path", flat=True))
        self.assertFalse({p for p in paths if not p.startswith("app/")}, paths)

    def test_a_folder_that_is_not_a_checkout_is_pinned_by_what_was_read(self):
        repository = self.register()
        self.assertTrue(index_repository(repository))
        repository.refresh_from_db()
        snapshot = repository.snapshots.get()
        self.assertEqual(snapshot.commit_sha, snapshot.manifest_digest)
        self.assertEqual(repository.default_ref, "working tree")
        # The same files again are the same snapshot, not a second one.
        repository = register_local(self.owner, self.app.pk, str(self.service))
        self.assertTrue(index_repository(repository))
        self.assertEqual(repository.snapshots.count(), 1)

    def test_the_roots_are_checked_again_when_the_index_runs(self):
        repository = self.register()
        with override_settings(CODE_GRAPH_LOCAL_ROOTS=[str(self.outside)]):
            self.assertTrue(index_repository(repository))
        repository.refresh_from_db()
        self.assertEqual(repository.status, "failed")
        self.assertIn("outside the folders", repository.error)
        self.assertFalse(repository.snapshots.exists())

    def test_check_branch_reads_the_checkout_and_reports_drift(self):
        git(self.service, refs={"refs/heads/main": SHA})
        repository = self.register()
        self.assertTrue(index_repository(repository))
        repository.refresh_from_db()
        refresh_head(repository)
        repository.refresh_from_db()
        self.assertIs(repository.drifted, False)
        write(self.service / ".git/refs/heads/main", LATER + "\n")
        refresh_head(repository)
        repository.refresh_from_db()
        self.assertEqual(repository.head_sha, LATER)
        self.assertIs(repository.drifted, True)

    def test_the_page_shows_the_folder_not_a_github_link(self):
        repository = self.register()
        body = self.client.get(
            f"{reverse('code-graph', args=[self.app.pk])}?repository={repository.pk}"
        ).content.decode()
        self.assertIn(str(self.service), body)
        self.assertNotIn("Open GitHub", body)


    # ------------------------------------------------------------- browsing

    def browse(self, path=None):
        url = reverse("code-graph-browse", args=[self.app.pk])
        return self.client.get(url, {"path": path} if path is not None else {})

    def test_the_add_form_offers_browse(self):
        page = self.client.get(reverse("code-graph-add", args=[self.app.pk]))
        self.assertContains(page, reverse("code-graph-browse", args=[self.app.pk]))

    def test_browse_starts_at_the_root_and_lists_only_what_could_be_read(self):
        git(self.service, refs={"refs/heads/main": SHA})
        write(self.root / "node_modules/x.js", "")
        write(self.root / ".cache/x", "")
        body = self.browse().content.decode()
        self.assertIn(">service<", body)
        self.assertIn(">git<", body)
        for hidden in ("node_modules", ".cache"):
            self.assertNotIn(hidden, body)
        # The root has nothing above it to offer.
        self.assertNotIn("Up</a>", body)

    def test_browse_goes_down_and_up_but_never_into_credentials(self):
        body = self.browse(str(self.service)).content.decode()
        self.assertIn(">app<", body)
        self.assertNotIn(">secrets<", body)
        self.assertNotIn(">node_modules<", body)
        self.assertIn("Up</a>", body)
        self.assertIn(f'name="folder" value="{self.service}"', body)
        refused = self.browse(str(self.secrets))
        self.assertContains(refused, "holds credentials")
        self.assertNotContains(refused, "jira")

    def test_browse_outside_the_roots_lists_nothing_from_there(self):
        page = self.browse(str(self.outside))
        self.assertContains(page, "outside the folders this server may read")
        self.assertNotContains(page, "private")

    def test_browse_is_for_people_who_can_register_and_only_when_enabled(self):
        self.client.force_login(self.viewer)
        self.assertEqual(self.browse().status_code, 403)
        self.client.force_login(self.owner)
        with override_settings(CODE_GRAPH_LOCAL_ROOTS=[]):
            self.assertEqual(self.browse().status_code, 404)

    def test_index_from_browse_posts_the_ordinary_register_form(self):
        response = self.client.post(
            reverse("code-graph-add", args=[self.app.pk]),
            {"action": "register_folder", "folder": str(self.service)},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(CodeRepository.objects.get().provider, "local")


class HeadTests(SimpleTestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        roots = override_settings(CODE_GRAPH_LOCAL_ROOTS=[str(self.root)])
        roots.enable()
        self.addCleanup(roots.disable)

    def test_packed_refs_and_detached_heads(self):
        git(self.root, packed=f"# pack-refs\n{SHA} refs/heads/main\n")
        self.assertEqual(code_graph_local.head_of(self.root), (SHA, "main"))
        git(self.root, head=LATER + "\n")
        self.assertEqual(code_graph_local.head_of(self.root), (LATER, ""))

    def test_a_ref_that_climbs_out_of_the_git_directory_is_ignored(self):
        git(self.root, head="ref: refs/../../escape\n")
        self.assertEqual(code_graph_local.head_of(self.root), ("", ""))

    def test_a_worktree_pointer_outside_the_roots_is_not_followed(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            git(Path(elsewhere), refs={"refs/heads/main": SHA})
            write(self.root / ".git", f"gitdir: {Path(elsewhere) / '.git'}\n")
            self.assertEqual(code_graph_local.head_of(self.root), ("", ""))

    def test_a_worktree_inside_the_roots_reads_the_shared_refs(self):
        main = self.root / "main"
        git(main, refs={"refs/heads/feature": SHA})
        worktree = main / ".git/worktrees/feature"
        write(worktree / "HEAD", "ref: refs/heads/feature\n")
        write(worktree / "commondir", "../..\n")
        tree = self.root / "feature"
        write(tree / ".git", f"gitdir: {worktree}\n")
        self.assertEqual(code_graph_local.head_of(tree), (SHA, "feature"))

    def test_a_path_with_unusual_characters_survives_the_round_trip(self):
        folder = self.root / "50%41 done #1"
        folder.mkdir()

        class Row:
            source_url = folder.as_uri()

        self.assertEqual(code_graph_local.path_of(Row), folder)


@override_settings(
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    CODE_FACTORY_LOCAL_WRITE=True,
)
class LocalDeliveryTests(TestCase):
    """Code Factory reading a folder, and writing an approved change back into it."""

    def setUp(self):
        from platform_core.models import ApplicationGrant, ChangePlan, FactoryRun, ProposedChange

        LocalFolderTests.setUp(self)
        (self.service / "app/util.py").write_bytes(b"def helper():\r\n    pass\r\n")
        ApplicationGrant.objects.filter(application=self.app, user=self.owner).update(
            can_approve=True
        )
        self.repository = register_local(self.owner, self.app.pk, str(self.service))
        self.assertTrue(index_repository(self.repository))
        self.snapshot = self.repository.snapshots.get()
        self.plan = ChangePlan.objects.create(
            application=self.app, author=self.owner, title="Fix", proposal="p",
            validation="v", digest="d", status="approved",
        )
        self.run = FactoryRun.objects.create(
            application=self.app, requested_by=self.owner, plan=self.plan,
            ticket_external_id="CARE-1", proposed_repository="service",
            code_snapshot=self.snapshot, status="prepared",
        )
        ProposedChange.objects.create(
            run=self.run, path="app/util.py", content="def helper():\n    return 1\n"
        )
        ProposedChange.objects.create(
            run=self.run, path="tests/test_util.py", content="def test_helper():\n    pass\n"
        )

    def apply(self):
        from platform_core.code_factory_build import apply_local

        return apply_local(self.owner, self.app.pk, self.run.pk)

    def test_the_change_lands_in_the_folder_with_its_line_endings(self):
        self.apply()
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "delivered")
        self.assertEqual(self.run.delivered_to, str(self.service))
        self.assertEqual(self.run.get_status_display(), "Written to folder")
        self.assertEqual(
            (self.service / "app/util.py").read_bytes(), b"def helper():\r\n    return 1\r\n"
        )
        self.assertTrue((self.service / "tests/test_util.py").is_file())

    def test_nothing_is_written_unless_the_operator_allows_it(self):
        with override_settings(CODE_FACTORY_LOCAL_WRITE=False):
            with self.assertRaisesMessage(ValidationError, "code_factory_local_write"):
                self.apply()
        self.assertFalse((self.service / "tests").exists())

    def test_a_file_changed_since_the_snapshot_stops_every_write(self):
        (self.service / "app/util.py").write_text("def helper():\n    return 2\n")
        with self.assertRaisesMessage(ValidationError, "changed since snapshot"):
            self.apply()
        self.assertFalse((self.service / "tests").exists())
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "prepared")

    def test_a_path_out_of_the_folder_or_into_git_is_refused(self):
        from platform_core.models import ProposedChange

        for path in ("../escape.py", ".git/hooks/post-checkout", "C:/x.py", "a/../../b.py"):
            with self.subTest(path=path):
                ProposedChange.objects.filter(run=self.run).delete()
                ProposedChange.objects.create(run=self.run, path=path, content="x")
                with self.assertRaisesMessage(ValidationError, "not a path"):
                    self.apply()
        self.assertFalse((self.base / "escape.py").exists())

    def test_a_github_write_credential_never_redirects_a_folder_run(self):
        """With one set, every read used to go to GitHub, to a different repository."""
        from platform_core.code_factory_build import gate

        with patch("platform_core.code_factory_build.write_credential", return_value="tok"):
            _, _, token = gate(self.owner, self.app.pk, self.run.pk)
        self.assertEqual(token, "")

    def test_a_folder_run_never_reaches_github_from_reading_to_refreshing(self):
        """Every step that can talk to GitHub, with a credential set and GitHub refused."""
        from platform_core.code_factory_build import (
            readable_targets,
            refresh_after,
            repository_test_setup,
            run_verification,
        )
        from platform_core.models import PlanItem

        PlanItem.objects.create(
            plan=self.plan, sequence=0, category="stated", title="t", explanation="e",
            change_summary="c", targets=["app/util.py"],
        )
        refused = AssertionError("GitHub must not be contacted")
        with (
            patch("platform_core.code_factory_build.write_credential", return_value="tok"),
            patch("platform_core.github_write.call", side_effect=refused),
            patch("platform_core.fetching.fetch", side_effect=refused),
            patch("platform_core.code_graph_clone.clone_sources", side_effect=refused),
        ):
            from platform_core.code_factory_build import gate

            _, _, token = gate(self.owner, self.app.pk, self.run.pk)
            files = readable_targets(self.run, token)
            self.assertEqual([item["path"] for item in files], ["app/util.py"])
            repository_test_setup(self.run, token)
            run_verification(
                self.run, [{"path": "app/util.py", "content": "x\n", "sha": None}], token
            )
            self.apply()
            refresh_after(self.owner, self.app.pk, self.run.pk, ["code", "knowledge"])
            self.repository.refresh_from_db()
            refresh_head(self.repository)
            self.assertTrue(index_repository(self.repository))

    def test_refreshing_everything_skips_an_uploaded_folder_instead_of_failing(self):
        from platform_core.code_factory_build import REFRESH_KEYS, refresh_after
        from platform_core.models import KnowledgeSource

        KnowledgeSource.objects.create(
            application=self.app, added_by=self.owner, provider="folder", name="docs",
            url="folder:docs", status="uploaded",
        )
        self.apply()
        notes = refresh_after(self.owner, self.app.pk, self.run.pk, list(REFRESH_KEYS))
        self.assertTrue(any("upload them again" in line for line in notes))
        self.repository.refresh_from_db()
        self.assertEqual(self.repository.status, "queued")

    def test_the_run_shows_before_and_after_once_the_refresh_lands(self):
        from platform_core.code_factory_build import before_after, refresh_after

        write(self.service / "app/main.py", "from app import util\nfrom app import extra\n")
        write(self.service / "app/extra.py", "X = 1\n")
        self.repository = register_local(self.owner, self.app.pk, str(self.service))
        self.assertTrue(index_repository(self.repository))
        self.run.code_snapshot = self.repository.snapshots.first()
        self.run.save(update_fields=["code_snapshot"])
        before = self.run.code_snapshot
        self.apply()
        self.assertIsNone(before_after(self.run))  # nothing asked for yet
        refresh_after(self.owner, self.app.pk, self.run.pk, ["code"])
        self.run.refresh_from_db()
        waiting = before_after(self.run)
        self.assertTrue(waiting["pending"])
        page = self.client.get(reverse("run-detail", args=[self.app.pk, self.run.pk]))
        self.assertContains(page, "data-live-pending")
        self.repository.refresh_from_db()
        self.assertTrue(index_repository(self.repository))
        shown = before_after(self.run)
        self.assertFalse(shown["pending"])
        self.assertEqual(shown["before"].pk, before.pk)
        self.assertEqual(shown["after"].number, before.number + 1)
        self.assertIn("app/util.py", shown["changed"])
        self.assertIn("tests/test_util.py", shown["added"])
        page = self.client.get(reverse("run-detail", args=[self.app.pk, self.run.pk]))
        self.assertContains(page, "Before and after this run")
        self.assertContains(page, f"snapshot v{before.number + 1}")
        self.assertNotContains(page, "data-live-pending")

    def test_the_picture_draws_what_is_new_only_after_and_shares_positions(self):
        from platform_core.code_factory_build import change_picture

        before = self.snapshot
        write(self.service / "app/util.py", "from app import extra\n")
        write(self.service / "app/extra.py", "X = 1\n")
        self.repository = register_local(self.owner, self.app.pk, str(self.service))
        self.assertTrue(index_repository(self.repository))
        after = self.repository.snapshots.first()
        was = set(before.relationships.values_list("source__path", "target__path", "kind"))
        now = set(after.relationships.values_list("source__path", "target__path", "kind"))

        def fake_links(revision, snapshot, node_id=None, **_):
            # T-1 reaches util.py only once the change exists.
            return ([{"path": "app/util.py"}] if snapshot == after else []), False

        connections = [{"items": [{"node_id": "n1", "node_label": "#: T-1"}]}]
        with patch("platform_core.code_knowledge.links", fake_links):
            picture = change_picture(
                object(), before, after, ["app/util.py"], was, now, connections
            )
        labels = [node["label"] for node in picture["nodes"]]
        self.assertEqual(labels[:2], ["T-1", "util.py"])
        self.assertIn("extra.py", labels)
        self.assertTrue(next(n for n in picture["nodes"] if n["label"] == "extra.py")["created"])
        self.assertTrue(all(edge["state"] != "new" for edge in picture["before"]))
        new = [edge for edge in picture["after"] if edge["state"] == "new"]
        self.assertEqual({edge["kind"] for edge in new}, {"code", "link"})
        self.assertEqual(picture["new_count"], len(new))

    def test_a_viewer_cannot_write(self):
        from django.core.exceptions import PermissionDenied

        from platform_core.code_factory_build import apply_local

        with self.assertRaises(PermissionDenied):
            apply_local(self.viewer, self.app.pk, self.run.pk)

    def test_refresh_re_indexes_the_folder_that_changed(self):
        from platform_core.code_factory_build import refresh_after

        self.apply()
        refresh_after(self.owner, self.app.pk, self.run.pk, ["code"])
        self.repository.refresh_from_db()
        self.assertEqual(self.repository.status, "queued")
        self.assertTrue(index_repository(self.repository))
        self.assertEqual(self.repository.snapshots.count(), 2)
        self.assertIn(
            "return 1",
            self.repository.snapshots.first().files.get(path="app/util.py").content,
        )

    def test_the_run_page_offers_writing_to_the_folder(self):
        page = self.client.get(reverse("run-detail", args=[self.app.pk, self.run.pk]))
        self.assertContains(page, 'value="apply-local"')
        self.assertNotContains(page, 'value="publish"')
        response = self.client.post(
            reverse("run-detail", args=[self.app.pk, self.run.pk]), {"action": "apply-local"}
        )
        self.assertEqual(response.status_code, 302)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "delivered")

    def test_a_ticket_naming_the_repository_pins_the_folder_of_that_name(self):
        from platform_core.code_factory import pin_repository
        from platform_core.models import FactoryRun

        run = FactoryRun.objects.create(
            application=self.app, requested_by=self.owner, ticket_external_id="CARE-2", number=2
        )
        pin_repository(run, "acme/service")
        run.refresh_from_db()
        self.assertEqual(run.code_snapshot_id, self.snapshot.pk)
        self.assertEqual(run.proposed_repository, "service")

    def test_production_refuses_local_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                f'mode = "production"\nhosts = ["h"]\n'
                f'secret_directory = "{Path(directory).as_posix()}"\n'
                "code_factory_local_write = true\n",
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"DIGITAL_BRAIN_CONFIG": str(path)}):
                with self.assertRaises(ImproperlyConfigured):
                    load_config()


class FolderRunWordingTests(LocalDeliveryTests):
    """A folder run never reads as a pull request, or as unwritten once written."""

    def test_the_strip_and_summary_say_folder(self):
        stages = {stage["number"]: stage["label"] for stage in self.run.stages}
        self.assertEqual(stages[5], "Write to folder")
        self.apply()
        self.run.refresh_from_db()
        page = self.client.get(reverse("run-detail", args=[self.app.pk, self.run.pk]))
        self.assertContains(page, "file(s) written</strong>")
        self.assertNotContains(page, "Nothing has been written yet")

    def test_the_agent_row_reports_the_folder_write(self):
        self.apply()
        self.run.refresh_from_db()
        page = self.client.get(reverse("run-detail", args=[self.app.pk, self.run.pk]))
        self.assertContains(page, "<strong>Write to folder</strong>")
        self.assertContains(page, f"Wrote 2 file(s) into {self.service}")
        self.assertNotContains(page, "<strong>Pull request</strong>")

    def test_the_enlarged_drawing_is_a_page_of_its_own(self):
        url = reverse("run-before-after", args=[self.app.pk, self.run.pk])
        self.assertEqual(self.client.get(url).status_code, 404)  # nothing to show yet


@override_settings(CODE_FACTORY_LOCAL_TESTS=True)
class LocalTestRunTests(LocalDeliveryTests):
    """Running a folder's own suite after writing into it - the one place code runs."""

    def setUp(self):
        super().setUp()
        type(self.snapshot).objects.filter(pk=self.snapshot.pk).update(
            test_setup={"python": "pytest"}
        )
        (self.service / ".venv" / "Scripts").mkdir(parents=True)
        (self.service / ".venv" / "Scripts" / "python.exe").write_bytes(b"")

    def run_tests(self, returncode=0, stdout="115 passed in 0.60s\n"):
        import subprocess

        from platform_core.code_factory_build import run_local_tests

        calls = []

        def fake_run(arguments, **options):
            calls.append((arguments, options))
            return subprocess.CompletedProcess(arguments, returncode, stdout=stdout, stderr="")

        with (
            patch("subprocess.run", fake_run),
            patch.dict("os.environ", {"GITHUB_TOKEN": "leak", "PATH": "p"}),
        ):
            result = run_local_tests(self.owner, self.app.pk, self.run.pk)
        return result, calls

    def test_nothing_runs_before_the_change_is_written(self):
        with self.assertRaisesMessage(ValidationError, "Write the change"):
            self.run_tests()

    def test_nothing_runs_unless_the_operator_allows_it(self):
        self.apply()
        with override_settings(CODE_FACTORY_LOCAL_TESTS=False):
            with self.assertRaisesMessage(ValidationError, "code_factory_local_tests"):
                self.run_tests()

    def test_a_pass_is_recorded_where_ci_would_be(self):
        self.apply()
        (conclusion, summary), calls = self.run_tests()
        self.assertEqual((conclusion, summary), ("success", "115 passed in 0.60s"))
        arguments, options = calls[0]
        self.assertTrue(arguments[0].endswith("python.exe"))
        self.assertEqual(arguments[1:4], ["-m", "pytest", "-q"])
        self.assertEqual(options["cwd"], self.service)
        self.assertNotIn("GITHUB_TOKEN", options["env"])
        self.run.refresh_from_db()
        self.assertEqual(self.run.checks_state, "ok")
        page = self.client.get(reverse("run-detail", args=[self.app.pk, self.run.pk]))
        self.assertContains(page, "115 passed in 0.60s")
        self.assertContains(page, "Run the tests again")

    def test_a_failure_is_a_failed_check(self):
        self.apply()
        (conclusion, _), _ = self.run_tests(returncode=1, stdout="1 failed, 114 passed\n")
        self.assertEqual(conclusion, "failure")
        self.run.refresh_from_db()
        self.assertEqual(self.run.checks_state, "failed")

    def test_production_refuses_running_tests(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                f'mode = "production"\nhosts = ["h"]\n'
                f'secret_directory = "{Path(directory).as_posix()}"\n'
                "code_factory_local_tests = true\n",
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"DIGITAL_BRAIN_CONFIG": str(path)}):
                with self.assertRaises(ImproperlyConfigured):
                    load_config()
