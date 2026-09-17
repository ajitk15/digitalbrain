"""Build B: implementation, verification and opening a pull request.

This is the only place the platform writes outside its own database, so most of
what matters here is what it refuses to do.
"""

import json
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import SimpleTestCase, TestCase, override_settings

from platform_core.code_factory import collect_changes, deliver, target_paths
from platform_core.github_write import (
    BRANCH_PREFIX,
    absent_path,
    commit_file,
    create_branch,
    safe_path,
)
from platform_core.models import ApplicationGrant, ChangePlan, FactoryRun, PlanItem

from . import test_documents

SETTINGS = dict(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)


class PathSafetyTests(SimpleTestCase):
    """Paths come from a model, so they are refused rather than cleaned."""

    def test_an_escaping_path_is_refused(self):
        for path in ("../secrets", "/etc/passwd", "a/../../b", ".git/config", ".git", ""):
            with self.subTest(path=path):
                with self.assertRaises(ValidationError):
                    safe_path(path)

    def test_an_ordinary_path_survives(self):
        self.assertEqual(safe_path("src/queue.py"), "src/queue.py")
        self.assertEqual(safe_path("a\\b.esql"), "a/b.esql")

    def test_a_branch_outside_the_prefix_is_refused(self):
        """A delivery must not be able to land on main."""
        with self.assertRaises(ValidationError):
            create_branch("o/r", "main", "sha", "token")
        with self.assertRaises(ValidationError):
            commit_file("o/r", "main", "a.py", "x", "sha", "m", "token")

    def test_creating_a_file_is_refused_unless_it_is_asked_for_by_name(self):
        """A missing sha must never quietly become a new file."""
        with self.assertRaises(ValidationError):
            commit_file("o/r", f"{BRANCH_PREFIX}x", "new.py", "x", "", "m", "token")

    def test_a_creation_carrying_a_sha_is_refused(self):
        """The caller would be saying the path both does and does not exist."""
        with self.assertRaises(ValidationError):
            commit_file(
                "o/r", f"{BRANCH_PREFIX}x", "new.py", "x", "sha", "m", "token", create=True
            )

    def test_an_asked_for_creation_sends_no_sha(self):
        """GitHub refuses a sha-less PUT to a path that exists. That is the backstop."""
        with patch("platform_core.github_write.call") as call:
            commit_file("o/r", f"{BRANCH_PREFIX}x", "new.py", "x", None, "m", "tok", create=True)
        self.assertNotIn("sha", call.call_args.kwargs["payload"])


class AbsentPathTests(SimpleTestCase):
    """Telling "not there" apart from "could not be read".

    read_file returns None for a file that is too large, binary, or a directory
    as well as for one that does not exist. Creating over any of those would be
    a silent overwrite of something nobody read, so only a 404 counts.
    """

    def failure(self, status):
        error = ValidationError("GitHub read: nope")
        error.status = status
        return error

    def test_only_a_404_means_the_path_is_not_there(self):
        for status, expected in ((404, "src/new.py"), (403, None), (500, None), (None, None)):
            with self.subTest(status=status):
                with patch(
                    "platform_core.github_write.call", side_effect=self.failure(status)
                ):
                    self.assertEqual(absent_path("o/r", "src/new.py", "main", "tok"), expected)

    def test_a_path_that_reads_back_is_not_absent(self):
        with patch("platform_core.github_write.call", return_value={"type": "file"}):
            self.assertIsNone(absent_path("o/r", "src/new.py", "main", "tok"))

    def test_a_path_that_is_not_a_path_is_not_absent(self):
        """A refused path is not a place to write; it is not a place at all."""
        with patch("platform_core.github_write.call") as call:
            self.assertIsNone(absent_path("o/r", "../escape", "main", "tok"))
        call.assert_not_called()


class ChangeCollectionTests(SimpleTestCase):
    def files(self):
        return [{"path": "a.py", "text": "original", "sha": "sha-a"}]

    def test_an_unchanged_file_is_not_committed(self):
        """Committing it would put an empty diff in front of a reviewer."""
        with self.assertRaises(ValidationError):
            collect_changes({"files": [{"file": "1", "content": "original"}]}, self.files())

    def test_a_file_the_model_invented_is_ignored(self):
        with self.assertRaises(ValidationError):
            collect_changes({"files": [{"file": "9", "content": "new"}]}, self.files())

    def test_a_real_change_is_kept_with_the_sha_it_was_read_at(self):
        changes = collect_changes({"files": [{"file": "1", "content": "new"}]}, self.files())
        self.assertEqual(changes, [{"path": "a.py", "content": "new", "sha": "sha-a"}])

    def test_an_empty_body_is_not_a_change(self):
        with self.assertRaises(ValidationError):
            collect_changes({"files": [{"file": "1", "content": "   "}]}, self.files())

    def test_a_new_file_is_collected_with_no_sha(self):
        """None is what later tells delivery to create rather than replace."""
        entry = [{"path": "tests/test_queue.py", "text": "", "sha": None}]
        payload = {"files": [{"file": "1", "content": "def test_it(): assert True"}]}
        changes = collect_changes(payload, entry)
        self.assertEqual(changes[0]["sha"], None)
        self.assertEqual(changes[0]["path"], "tests/test_queue.py")


@override_settings(**SETTINGS)
class DeliveryGateTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        ApplicationGrant.objects.filter(application=self.app, user=self.owner).update(
            can_approve=True
        )
        self.plan = ChangePlan.objects.create(
            application=self.app,
            author=self.viewer,
            title="Bound retries",
            proposal="p",
            validation="v",
            digest="d",
            status="approved",
        )
        PlanItem.objects.create(
            plan=self.plan,
            sequence=0,
            category="stated",
            title="Bound retries",
            explanation="e",
            change_summary="c",
            targets=["src/queue.py"],
        )
        self.run = FactoryRun.objects.create(
            application=self.app,
            requested_by=self.owner,
            plan=self.plan,
            ticket_external_id="OPS-1",
            proposed_repository="acme/widgets",
            repository_confirmed=True,
            status="awaiting_review",
        )

    def deliver(self, user=None):
        return deliver(user or self.owner, self.app.pk, self.run.pk)

    def test_an_unapproved_plan_cannot_be_delivered(self):
        ChangePlan.objects.filter(pk=self.plan.pk).update(status="pending")
        with self.assertRaises(ValidationError) as raised:
            self.deliver()
        self.assertIn("Approve the plan", " ".join(raised.exception.messages))

    def test_an_unconfirmed_repository_cannot_be_delivered_to(self):
        """The repository came from ticket text; a person has to say yes."""
        FactoryRun.objects.filter(pk=self.run.pk).update(repository_confirmed=False)
        self.run.refresh_from_db()
        with self.assertRaises(ValidationError) as raised:
            self.deliver()
        self.assertIn("Confirm the repository", " ".join(raised.exception.messages))

    def test_delivery_needs_the_approval_permission(self):
        with self.assertRaises(PermissionDenied):
            self.deliver(self.viewer)

    def test_the_read_only_credential_is_not_used_for_writing(self):
        with patch("platform_core.code_factory.write_credential", return_value=""):
            with self.assertRaises(ValidationError) as raised:
                self.deliver()
        message = " ".join(raised.exception.messages)
        self.assertIn(f"github_write_{self.app.pk}", message)
        self.assertIn("read-only connector credential", message)

    def test_a_run_cannot_open_a_second_pull_request(self):
        FactoryRun.objects.filter(pk=self.run.pk).update(
            pull_request_url="https://github.com/acme/widgets/pull/1"
        )
        self.run.refresh_from_db()
        with self.assertRaises(ValidationError) as raised:
            self.deliver()
        self.assertIn("already opened", " ".join(raised.exception.messages))

    def test_nothing_is_written_when_no_named_file_can_be_read(self):
        """Unreadable is not absent: a file too large or binary is not created over."""
        with patch("platform_core.code_factory.write_credential", return_value="tok"):
            with patch("platform_core.github_write.read_file", return_value=None):
                with patch("platform_core.github_write.absent_path", return_value=None):
                    with patch("platform_core.github_write.create_branch") as branch:
                        with self.assertRaises(ValidationError):
                            self.deliver()
        branch.assert_not_called()
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "failed")
        self.assertEqual(self.run.phases.get(name="implementation").status, "failed")

    def test_an_elided_file_is_refused_before_anything_is_written(self):
        """A model that abbreviates would otherwise delete the rest of the file."""
        current = {"path": "src/queue.py", "text": "original code", "sha": "sha1"}
        answer = json.dumps({"files": [{"file": "1", "content": "def go():\n    # ... rest of"}]})
        with patch("platform_core.code_factory.write_credential", return_value="tok"):
            with patch("platform_core.github_write.read_file", return_value=current):
                with patch("platform_core.ai.invoke_ai", return_value=answer):
                    with patch("platform_core.github_write.create_branch") as branch:
                        with self.assertRaises(ValidationError) as raised:
                            self.deliver()
        branch.assert_not_called()
        self.assertIn("elision", " ".join(raised.exception.messages))
        self.assertEqual(self.run.phases.get(name="verification").status, "failed")

    def test_a_file_that_moved_since_it_was_read_is_refused(self):
        reads = [
            {"path": "src/queue.py", "text": "original", "sha": "sha1"},
            {"path": "src/queue.py", "text": "someone else changed it", "sha": "sha2"},
        ]
        answer = json.dumps({"files": [{"file": "1", "content": "new content"}]})
        with patch("platform_core.code_factory.write_credential", return_value="tok"):
            with patch("platform_core.github_write.read_file", side_effect=reads):
                with patch("platform_core.ai.invoke_ai", return_value=answer):
                    with patch("platform_core.github_write.create_branch") as branch:
                        with self.assertRaises(ValidationError) as raised:
                            self.deliver()
        branch.assert_not_called()
        self.assertIn("changed in the repository", " ".join(raised.exception.messages))

    def test_a_clean_change_opens_a_draft_pull_request(self):
        current = {"path": "src/queue.py", "text": "original", "sha": "sha1"}
        answer = json.dumps({"files": [{"file": "1", "content": "bounded"}]})
        with patch("platform_core.code_factory.write_credential", return_value="tok"):
            with patch("platform_core.github_write.read_file", return_value=current):
                with patch("platform_core.ai.invoke_ai", return_value=answer):
                    with patch("platform_core.github_write.branch_head", return_value="head"):
                        with patch("platform_core.github_write.create_branch") as branch:
                            with patch("platform_core.github_write.commit_file") as commit:
                                with patch(
                                    "platform_core.github_write.open_pull_request",
                                    return_value="https://github.com/acme/widgets/pull/7",
                                ) as pull:
                                    url = self.deliver()
        self.assertEqual(url, "https://github.com/acme/widgets/pull/7")
        self.assertTrue(branch.call_args.args[1].startswith(BRANCH_PREFIX))
        self.assertEqual(commit.call_args.args[2], "src/queue.py")
        # Draft, and against the confirmed base rather than whatever was default.
        self.assertTrue(pull.call_args.args)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "delivered")
        self.assertEqual(self.run.pull_request_url, url)
        self.assertEqual(self.run.phases.get(name="delivery").status, "ok")

    def new_file_run(self, absent, answer=None, **extra):
        """Deliver a plan whose one named path is not in the repository."""
        answer = answer or json.dumps(
            {"files": [{"file": "1", "content": "def test_bound(): assert True"}]}
        )
        patches = {
            "platform_core.code_factory.write_credential": {"return_value": "tok"},
            "platform_core.github_write.read_file": {"return_value": None},
            "platform_core.github_write.absent_path": absent,
            "platform_core.ai.invoke_ai": {"return_value": answer},
            "platform_core.github_write.branch_head": {"return_value": "head"},
            "platform_core.github_write.create_branch": {},
            "platform_core.github_write.commit_file": {},
            "platform_core.github_write.open_pull_request": {
                "return_value": "https://github.com/acme/widgets/pull/9"
            },
        }
        patches.update(extra)
        started = [patch(target, **options) for target, options in patches.items()]
        mocks = {target: item.start() for target, item in zip(patches, started, strict=True)}
        self.addCleanup(lambda: [item.stop() for item in started])
        return mocks

    def test_a_named_path_that_is_not_there_is_written_rather_than_skipped(self):
        """A fix is not only an edit. The test that proves it is a file that is not there yet."""
        mocks = self.new_file_run({"return_value": "tests/test_queue.py"})
        url = self.deliver()
        self.assertEqual(url, "https://github.com/acme/widgets/pull/9")
        commit = mocks["platform_core.github_write.commit_file"]
        self.assertEqual(commit.call_args.args[2], "tests/test_queue.py")
        self.assertIsNone(commit.call_args.args[4])
        self.assertTrue(commit.call_args.kwargs["create"])
        phase = self.run.phases.get(name="implementation")
        self.assertTrue(phase.output["files"][0]["new"])

    def test_a_new_file_that_appeared_since_it_was_read_is_refused(self):
        """Somebody else adding that path in between is the staleness case for a creation."""
        mocks = self.new_file_run({"side_effect": ["tests/test_queue.py", None]})
        with self.assertRaises(ValidationError) as raised:
            self.deliver()
        self.assertIn("exists now", " ".join(raised.exception.messages))
        mocks["platform_core.github_write.create_branch"].assert_not_called()
        self.assertEqual(self.run.phases.get(name="verification").status, "failed")

    def test_the_pull_request_names_the_files_it_creates(self):
        """Adding a file is the more consequential half; a reviewer is told before reading."""
        mocks = self.new_file_run({"return_value": "tests/test_queue.py"})
        self.deliver()
        body = mocks["platform_core.github_write.open_pull_request"].call_args.args[4]
        self.assertIn("New files in this change:", body)
        self.assertIn("tests/test_queue.py", body)

    def test_opening_a_pull_request_is_audited(self):
        from platform_core.models import AuditEvent

        current = {"path": "src/queue.py", "text": "original", "sha": "sha1"}
        answer = json.dumps({"files": [{"file": "1", "content": "bounded"}]})
        with patch("platform_core.code_factory.write_credential", return_value="tok"):
            with patch("platform_core.github_write.read_file", return_value=current):
                with patch("platform_core.ai.invoke_ai", return_value=answer):
                    with patch("platform_core.github_write.branch_head", return_value="h"):
                        with patch("platform_core.github_write.create_branch"):
                            with patch("platform_core.github_write.commit_file"):
                                with patch(
                                    "platform_core.github_write.open_pull_request",
                                    return_value="https://github.com/acme/widgets/pull/7",
                                ):
                                    self.deliver()
        self.assertTrue(
            AuditEvent.objects.filter(action="factory.pull_request_opened").exists()
        )


class TargetPathTests(SimpleTestCase):
    def test_a_component_name_is_not_treated_as_a_path(self):
        """Design may name a component; that is not something to open."""

        class Item:
            def __init__(self, targets):
                self.targets = targets

        class Plan:
            def __init__(self, items):
                self._items = items

            @property
            def items(self):
                plan = self

                class Manager:
                    def exclude(self, **kwargs):
                        return self

                    def order_by(self, *args):
                        return plan._items

                return Manager()

        paths = target_paths(Plan([Item(["ACE_DEMO_CACHE integration server", "src/queue.py"])]))
        self.assertEqual(paths, ["src/queue.py"])
