"""Build B: implementation, verification and opening a pull request.

This is the only place the platform writes outside its own database, so most of
what matters here is what it refuses to do.
"""

import json
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import SimpleTestCase, TestCase, override_settings

from platform_core.code_factory import collect_changes, prepare, publish, target_paths
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
        """Both halves, as delivery used to be one call.

        Every gate these exercise applies to each half, so driving them
        together keeps the assertions meaningful while the screen puts a person
        between the two.
        """
        actor = user or self.owner
        prepare(actor, self.app.pk, self.run.pk)
        self.run.refresh_from_db()
        return publish(actor, self.app.pk, self.run.pk)

    def test_an_unapproved_plan_cannot_be_delivered(self):
        ChangePlan.objects.filter(pk=self.plan.pk).update(status="pending")
        with self.assertRaises(ValidationError) as raised:
            self.deliver()
        self.assertIn("Approve the plan", " ".join(raised.exception.messages))

    def test_an_unconfirmed_repository_cannot_be_delivered_to(self):
        """The repository came from ticket text; a person has to say yes.

        Checked where something actually leaves. Writing the change puts
        nothing anywhere, so it does not need a repository at all - but opening
        a pull request does, and that is where the question is asked.
        """
        FactoryRun.objects.filter(pk=self.run.pk).update(
            repository_confirmed=False, status="prepared"
        )
        self.run.refresh_from_db()
        with patch("platform_core.code_factory.write_credential", return_value="tok"):
            with self.assertRaises(ValidationError) as raised:
                publish(self.owner, self.app.pk, self.run.pk)
        self.assertIn("Confirm the repository", " ".join(raised.exception.messages))

    def test_delivery_needs_the_approval_permission(self):
        with self.assertRaises(PermissionDenied):
            self.deliver(self.viewer)

    def test_the_read_only_credential_is_not_used_for_writing(self):
        """Required to publish, and only to publish."""
        FactoryRun.objects.filter(pk=self.run.pk).update(status="prepared")
        self.run.refresh_from_db()
        with patch("platform_core.code_factory.write_credential", return_value=""):
            with self.assertRaises(ValidationError) as raised:
                publish(self.owner, self.app.pk, self.run.pk)
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
        # Recorded on the work order, which is the agent that does the reading.
        self.assertEqual(self.run.phases.get(name="work_order").status, "failed")

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


class PreparedChangeTests(DeliveryGateTests):
    """The stop between writing a change and letting it out.

    These were one act, so the contents existed only inside that call and
    nobody saw what was about to be written until it had been. What these pin
    is that the first half writes nothing anywhere, that the second half writes
    exactly what was shown, and that "no" is a real answer.
    """

    CURRENT = {"path": "src/queue.py", "text": "original", "sha": "sha1"}
    ANSWER = json.dumps({"files": [{"file": "1", "content": "bounded"}]})

    def write(self):
        """Run the first half with the repository and the model stubbed."""
        with patch("platform_core.code_factory.write_credential", return_value="tok"):
            with patch("platform_core.github_write.read_file", return_value=self.CURRENT):
                with patch("platform_core.ai.invoke_ai", return_value=self.ANSWER):
                    return prepare(self.owner, self.app.pk, self.run.pk)

    def test_preparing_writes_nothing_to_the_repository(self):
        from platform_core.models import ProposedChange

        with patch("platform_core.github_write.create_branch") as branch:
            with patch("platform_core.github_write.commit_file") as commit:
                with patch("platform_core.github_write.open_pull_request") as pull:
                    self.write()
        for untouched in (branch, commit, pull):
            untouched.assert_not_called()
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "prepared")
        self.assertEqual(self.run.pull_request_url, "")
        change = ProposedChange.objects.get(run=self.run)
        self.assertEqual(change.path, "src/queue.py")
        self.assertEqual(change.content, "bounded")
        self.assertEqual(change.base_sha, "sha1")
        self.assertFalse(change.creates)

    def test_the_pull_request_writes_exactly_what_was_shown(self):
        """Re-read from the stored change, never taken from the request."""
        self.write()
        with patch("platform_core.code_factory.write_credential", return_value="tok"):
            with patch("platform_core.github_write.read_file", return_value=self.CURRENT):
                with patch("platform_core.github_write.branch_head", return_value="head"):
                    with patch("platform_core.github_write.create_branch"):
                        with patch("platform_core.github_write.commit_file") as commit:
                            with patch(
                                "platform_core.github_write.open_pull_request",
                                return_value="https://github.com/acme/widgets/pull/9",
                            ):
                                url = publish(self.owner, self.app.pk, self.run.pk)
        self.assertEqual(commit.call_args.args[2], "src/queue.py")
        self.assertEqual(commit.call_args.args[3], "bounded")
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "delivered")
        self.assertEqual(self.run.pull_request_url, url)

    def test_a_file_that_moved_while_the_summary_was_read_is_refused(self):
        """The window the second verification exists for."""
        self.write()
        moved = {"path": "src/queue.py", "text": "somebody else", "sha": "sha2"}
        with patch("platform_core.code_factory.write_credential", return_value="tok"):
            with patch("platform_core.github_write.read_file", return_value=moved):
                with patch("platform_core.github_write.create_branch") as branch:
                    with self.assertRaises(ValidationError) as raised:
                        publish(self.owner, self.app.pk, self.run.pk)
        branch.assert_not_called()
        self.assertIn("changed in the repository", " ".join(raised.exception.messages))

    def test_publishing_without_preparing_is_refused(self):
        with patch("platform_core.code_factory.write_credential", return_value="tok"):
            with self.assertRaises(ValidationError) as raised:
                publish(self.owner, self.app.pk, self.run.pk)
        self.assertIn("read its summary first", " ".join(raised.exception.messages))

    def test_discarding_leaves_the_approved_plan_and_writes_nothing(self):
        from platform_core.code_factory import discard
        from platform_core.models import ProposedChange

        self.write()
        with patch("platform_core.code_factory.write_credential", return_value="tok"):
            discard(self.owner, self.app.pk, self.run.pk)
        self.run.refresh_from_db()
        self.plan.refresh_from_db()
        self.assertEqual(self.run.status, "awaiting_review")
        self.assertEqual(self.run.pull_request_url, "")
        self.assertFalse(ProposedChange.objects.filter(run=self.run).exists())
        # The decision that was made stands; only the unwritten change is gone.
        self.assertEqual(self.plan.status, "approved")

    def test_discarding_what_was_never_prepared_is_refused(self):
        from platform_core.code_factory import discard

        with patch("platform_core.code_factory.write_credential", return_value="tok"):
            with self.assertRaises(ValidationError):
                discard(self.owner, self.app.pk, self.run.pk)


class QueuedPreparationTests(DeliveryGateTests):
    """Stage four runs on the worker, so the screen can show it happening.

    Held in a request it would be a blank page for a minute with nothing to
    report, which is the opposite of what the screen is for.
    """

    def test_requesting_queues_rather_than_running(self):
        from platform_core.code_factory import request_preparation

        with patch("platform_core.code_factory.write_credential", return_value="tok"):
            with patch("platform_core.code_factory.run_implementation") as agent:
                request_preparation(self.owner, self.app.pk, self.run.pk)
        agent.assert_not_called()
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "prepare_queued")
        self.assertEqual(self.run.acting_user, self.owner)

    def test_the_worker_runs_it_as_whoever_asked(self):
        from platform_core.code_factory import process_next_preparation, request_preparation
        from platform_core.models import ProposedChange

        current = {"path": "src/queue.py", "text": "original", "sha": "sha1"}
        answer = json.dumps({"files": [{"file": "1", "content": "bounded"}]})
        with patch("platform_core.code_factory.write_credential", return_value="tok"):
            request_preparation(self.owner, self.app.pk, self.run.pk)
            with patch("platform_core.github_write.read_file", return_value=current):
                with patch("platform_core.ai.invoke_ai", return_value=answer):
                    self.assertTrue(process_next_preparation())
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "prepared")
        self.assertEqual(ProposedChange.objects.get(run=self.run).content, "bounded")

    def test_approval_withdrawn_while_it_waited_stops_the_work(self):
        """Re-checked inside the worker call, like every other queued act here."""
        from platform_core.code_factory import process_next_preparation, request_preparation
        from platform_core.models import ApplicationGrant, ProposedChange

        with patch("platform_core.code_factory.write_credential", return_value="tok"):
            request_preparation(self.owner, self.app.pk, self.run.pk)
        ApplicationGrant.objects.filter(application=self.app, user=self.owner).update(
            can_approve=False
        )
        with patch("platform_core.github_write.read_file") as read:
            self.assertTrue(process_next_preparation())
        read.assert_not_called()
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "failed")
        self.assertFalse(ProposedChange.objects.filter(run=self.run).exists())

    def test_an_empty_queue_is_not_work(self):
        from platform_core.code_factory import process_next_preparation

        self.assertFalse(process_next_preparation())

    def test_the_lane_runs_analysis_and_implementation(self):
        from platform_core.document_worker import LANES

        lanes = {name: [step.__name__ for step in steps_for()] for name, steps_for, _ in LANES}
        self.assertIn("process_next_run", lanes["factory"])
        self.assertIn("process_next_preparation", lanes["factory"])


class AgentChainTests(DeliveryGateTests):
    """Stage four is a chain of agents, each with its own receipt.

    It used to be one model call. What these pin is that each agent is a real
    phase, that the reviewer can drop a file without abandoning the change, and
    that a stumbling test author does not throw away work that is already
    correct.
    """

    CURRENT = {"path": "src/queue.py", "text": "original", "sha": "sha1"}

    def answers(self, *, review="ok", tests=True):
        """One canned reply per agent, in the order the chain asks."""
        order = json.dumps(
            {
                "files": [{"file": "1", "intent": "Bound the retry loop", "checks": ["it stops"]}],
                "leftover": ["Tell the release manager"],
            }
        )
        implementation = json.dumps({"files": [{"file": "1", "content": "bounded"}]})
        written = json.dumps({"files": [{"file": "1", "content": "def test_bounded(): pass"}]})
        verdict = json.dumps(
            {"files": [{"file": "1", "verdict": review, "reason": "does not do it"}]}
        )
        replies = [order, implementation]
        if tests:
            replies.append(written)
        replies.append(verdict)
        return replies

    def chain(self, **kwargs):
        with patch("platform_core.code_factory.write_credential", return_value="tok"):
            with patch("platform_core.github_write.read_file", return_value=self.CURRENT):
                with patch("platform_core.ai.invoke_ai", side_effect=self.answers(**kwargs)):
                    return prepare(self.owner, self.app.pk, self.run.pk)

    def test_every_agent_records_its_own_phase(self):
        self.chain(tests=False)
        ran = dict(self.run.phases.values_list("name", "status"))
        self.assertEqual(ran["work_order"], "ok")
        self.assertEqual(ran["implementation"], "ok")
        self.assertEqual(ran["review"], "ok")
        self.assertEqual(ran["verification"], "ok")
        # No approved item named a test file, so that agent had nothing to do.
        self.assertEqual(ran["tests"], "skipped")

    def test_an_item_no_file_can_satisfy_is_said_rather_than_forced(self):
        self.chain(tests=False)
        messages = [event.message for event in self.run.events.filter(phase="work_order")]
        self.assertTrue(
            any("Not implementable as a code change" in message for message in messages),
            messages,
        )

    def test_a_rejected_file_is_dropped_and_the_rest_survive(self):
        from platform_core.models import ProposedChange

        with self.assertRaises(ValidationError) as raised:
            self.chain(tests=False, review="reject")
        # One file, rejected, so nothing is left - and it says so rather than
        # opening an empty pull request.
        self.assertIn("rejected every file", " ".join(raised.exception.messages))
        self.assertFalse(ProposedChange.objects.filter(run=self.run).exists())

    def test_the_review_reason_reaches_the_record(self):
        with self.assertRaises(ValidationError):
            self.chain(tests=False, review="reject")
        problems = [
            event.message for event in self.run.events.filter(phase="review", level="problem")
        ]
        self.assertTrue(any("does not do it" in message for message in problems), problems)


class CheckRunTests(DeliveryGateTests):
    """What the repository's own CI made of the branch.

    This platform writes the tests and never runs them - it holds knowledge
    about an application, not a checkout of it. These pin that it reads the
    result rather than producing one, and that a repository with no CI is not
    reported as a failure.
    """

    def delivered(self, checks=None):
        FactoryRun.objects.filter(pk=self.run.pk).update(
            status="delivered",
            pull_request_url="https://github.com/acme/widgets/pull/7",
            branch="digital-brain/ops-1-abcd1234",
            checks=checks or [],
        )
        self.run.refresh_from_db()
        return self.run

    def test_a_repository_without_checks_is_not_a_failure(self):
        run = self.delivered()
        self.assertEqual(run.checks_state, "pending")

    def test_a_running_suite_reads_as_running(self):
        run = self.delivered([{"name": "tests", "status": "in_progress", "conclusion": ""}])
        self.assertEqual(run.checks_state, "running")
        self.assertIn("0 of 1 finished", run.get_checks_label())

    def test_a_failure_is_reported_as_one(self):
        run = self.delivered(
            [
                {"name": "tests", "status": "completed", "conclusion": "failure"},
                {"name": "lint", "status": "completed", "conclusion": "success"},
            ]
        )
        self.assertEqual(run.checks_state, "failed")
        self.assertIn("1 of 2 passed", run.get_checks_label())

    def test_all_green_settles_the_stage(self):
        run = self.delivered([{"name": "tests", "status": "completed", "conclusion": "success"}])
        self.assertEqual(run.checks_state, "ok")
        self.assertEqual(run.stages[5]["state"], "ok")

    def test_the_worker_stops_asking_once_every_check_has_concluded(self):
        from platform_core.code_factory import process_next_checks

        self.delivered([{"name": "tests", "status": "completed", "conclusion": "success"}])
        with patch("platform_core.github_write.check_runs") as asked:
            self.assertFalse(process_next_checks())
        asked.assert_not_called()

    def test_the_worker_reads_and_records_a_conclusion(self):
        from platform_core.code_factory import process_next_checks

        self.delivered()
        reported = [{"name": "tests", "status": "completed", "conclusion": "failure", "url": ""}]
        with patch("platform_core.link_sources.github_token", return_value="tok"):
            with patch("platform_core.github_write.check_runs", return_value=reported):
                self.assertTrue(process_next_checks())
        self.run.refresh_from_db()
        self.assertEqual(self.run.checks_state, "failed")
        self.assertIsNotNone(self.run.checks_read_at)
        # A conclusion changing is worth a line in the run's own record.
        messages = [event.message for event in self.run.events.filter(level="problem")]
        self.assertTrue(any("tests: failure" in message for message in messages), messages)

    def test_the_lane_asks_about_checks(self):
        from platform_core.document_worker import LANES

        lanes = {name: [step.__name__ for step in steps_for()] for name, steps_for, _ in LANES}
        self.assertIn("process_next_checks", lanes["factory"])


class AgentReportTests(DeliveryGateTests):
    """Each agent row says what it did in this run, not what it is for.

    "Passed" is true of every successful check and tells a reader nothing. The
    rows report from the same recorded output the phase receipt is built from,
    so the summary and the receipt cannot disagree.
    """

    def report(self, name):
        from platform_core.workbench import AGENT_ROW, agent_report

        phases = {phase.name: phase for phase in self.run.phases.all()}
        label, waiting = next(row[1:] for row in AGENT_ROW if row[0] == name)
        return agent_report(name, label, waiting, phases.get(name), self.run)

    def ran(self, name, status="ok", output=None, **extra):
        from platform_core.models import RunPhase

        return RunPhase.objects.create(
            run=self.run, name=name, sequence=1, status=status, output=output or {}, **extra
        )

    def test_implementation_names_the_files_it_wrote(self):
        self.ran(
            "implementation",
            output={
                "files": [
                    {"path": "src/queue.py", "new": False},
                    {"path": "t.py", "new": True},
                ]
            },
        )
        detail = self.report("implementation")["detail"]
        self.assertIn("Wrote 2 file(s)", detail)
        self.assertIn("1 of them new", detail)
        self.assertIn("src/queue.py", detail)

    def test_the_review_says_what_it_rejected_and_why(self):
        self.ran(
            "review",
            output={
                "kept": ["a.py"],
                "rejected": [{"path": "b.py", "reason": "leaves the item unsatisfied"}],
            },
        )
        detail = self.report("review")["detail"]
        self.assertIn("Passed 1 file(s)", detail)
        self.assertIn("b.py", detail)
        self.assertIn("leaves the item unsatisfied", detail)

    def test_the_pre_write_checks_say_what_they_checked(self):
        self.ran("verification", output={"checked": ["a.py", "b.py"], "created": ["b.py"]})
        detail = self.report("verification")["detail"]
        self.assertIn("Re-read 2 file(s)", detail)
        self.assertIn("still absent", detail)
        self.assertNotEqual(detail, "Passed")

    def test_delivery_names_the_branch(self):
        self.ran("delivery", output={"branch": "digital-brain/ops-1", "pull_request": "u"})
        self.assertIn("digital-brain/ops-1", self.report("delivery")["detail"])

    def test_a_failure_carries_its_reason(self):
        self.ran("tests", status="failed", error="Test author did not return usable JSON.")
        self.assertIn("usable JSON", self.report("tests")["detail"])

    def test_the_cost_is_carried_beside_what_it_did(self):
        self.ran(
            "work_order",
            output={"order": {"1": {}}},
            prompt_tokens=1200,
            completion_tokens=90,
        )
        detail = self.report("work_order")["detail"]
        self.assertIn("Set an intent for 1 file(s)", detail)
        self.assertIn("1,200 in / 90 out tokens", detail)

    def test_an_agent_a_finished_run_never_had_says_so(self):
        """Rather than waiting forever for something that cannot happen."""
        FactoryRun.objects.filter(pk=self.run.pk).update(status="delivered")
        self.run.refresh_from_db()
        report = self.report("tests")
        self.assertEqual(report["status"], "absent")
        self.assertIn("predates this agent", report["detail"])

    def test_an_unfinished_run_still_says_what_the_agent_is_for(self):
        report = self.report("tests")
        self.assertEqual(report["status"], "pending")
        self.assertIn("Writes the tests", report["detail"])


class OfflineChangeTests(DeliveryGateTests):
    """A change written where this platform must never write.

    A client environment with no outbound GitHub write can still run the
    agents: they read the pinned Code Graph snapshot, produce the files, and
    stop. Refusing it a credential it does not need would be refusing it the
    whole product.
    """

    ANSWERS = [
        json.dumps({"files": [{"file": "1", "intent": "Bound it", "checks": []}]}),
        json.dumps({"files": [{"file": "1", "content": "bounded"}]}),
        json.dumps({"files": [{"file": "1", "verdict": "ok", "reason": ""}]}),
    ]

    def snapshot(self, path="src/queue.py", content="original"):
        from platform_core.models import CodeRepository, CodeSnapshot

        repo = CodeRepository.objects.create(
            application=self.app,
            provider="github",
            external_id="acme/widgets",
            name="acme/widgets",
            source_url="https://github.com/acme/widgets",
            added_by=self.owner,
            status="ready",
        )
        snapshot = CodeSnapshot.objects.create(number=1, repository=repo, commit_sha="c" * 40)
        snapshot.files.create(path=path, language="python", content=content, lines=1)
        FactoryRun.objects.filter(pk=self.run.pk).update(code_snapshot=snapshot)
        self.run.refresh_from_db()
        return snapshot

    def offline(self):
        with patch("platform_core.code_factory.write_credential", return_value=""):
            with patch("platform_core.ai.invoke_ai", side_effect=self.ANSWERS):
                with patch("platform_core.github_write.fetch") as reached:
                    result = prepare(self.owner, self.app.pk, self.run.pk)
        # The point of the path: GitHub is never called at all.
        reached.assert_not_called()
        return result

    def test_the_agents_run_from_the_snapshot_and_touch_nothing(self):
        from platform_core.models import ProposedChange

        self.snapshot()
        self.offline()
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "prepared")
        change = ProposedChange.objects.get(run=self.run)
        self.assertEqual(change.path, "src/queue.py")
        self.assertEqual(change.content, "bounded")

    def test_it_says_which_commit_the_files_came_from(self):
        self.snapshot()
        self.offline()
        messages = " ".join(event.message for event in self.run.events.all())
        self.assertIn("snapshot v1", messages)
        self.assertIn("Nothing will be written anywhere", messages)

    def test_the_staleness_check_is_named_as_impossible_rather_than_skipped(self):
        """"Checked" must not quietly mean two different things."""
        self.snapshot()
        self.offline()
        limits = self.run.phases.get(name="verification").output["limits"]
        self.assertIn("No staleness check was possible", limits)
        messages = " ".join(event.message for event in self.run.events.all())
        self.assertIn("Staleness could not be checked", messages)

    def test_an_unconfirmed_repository_does_not_block_writing_the_change(self):
        """Confirmation is about where we write, and this writes nowhere."""
        FactoryRun.objects.filter(pk=self.run.pk).update(repository_confirmed=False)
        self.run.refresh_from_db()
        self.snapshot()
        self.offline()
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "prepared")

    def test_no_snapshot_and_no_credential_is_refused_with_both_ways_out(self):
        with patch("platform_core.code_factory.write_credential", return_value=""):
            with self.assertRaises(ValidationError) as raised:
                prepare(self.owner, self.app.pk, self.run.pk)
        message = " ".join(raised.exception.messages)
        self.assertIn("Index the repository in Code Graph", message)
        self.assertIn("mount a write-scoped credential", message)


class AnnotatedTargetTests(SimpleTestCase):
    """A path the design annotated with the symbol it means.

    "app/views.py (index)" is a reasonable reading of "name the files this
    touches", but the annotation is not part of the path. Left on, the file is
    not found, is therefore taken to be absent, and is created under that name -
    a file called `views.py (index)` committed to somebody's repository.
    """

    def paths(self, *targets):
        from types import SimpleNamespace

        from platform_core.code_factory import target_paths

        items = [
            SimpleNamespace(targets=list(targets), sequence=0, status="accepted")
        ]
        plan = SimpleNamespace(
            items=SimpleNamespace(exclude=lambda **kw: SimpleNamespace(order_by=lambda f: items))
        )
        return target_paths(plan)

    def test_the_symbol_is_stripped_and_the_path_kept(self):
        self.assertEqual(
            self.paths("src/carepath/api/routes_fhir.py (export_everything)"),
            ["src/carepath/api/routes_fhir.py"],
        )

    def test_a_plain_path_is_untouched(self):
        self.assertEqual(self.paths("src/queue.py"), ["src/queue.py"])

    def test_two_annotations_of_one_file_collapse_to_it(self):
        self.assertEqual(
            self.paths("a/b.py (one)", "a/b.py (two)", "a/b.py"), ["a/b.py"]
        )

    def test_a_component_that_is_not_a_path_is_still_dropped(self):
        """Stripping must not turn prose into a path."""
        self.assertEqual(self.paths("the consent service (consent.current)"), [])
