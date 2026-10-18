"""Background work is asked for by the change, not found by a scan.

Phase 1 of the scale work: a `Job` row is written in the same transaction as
the change that needs it, folds with any request already waiting, is claimed
with a lease, retried only where its kind allows, and put back when the worker
holding it goes away. The graph and connector lanes run on it; intake takes
turns across applications; the lanes that claim row by row may run several
threads.
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.db import transaction
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from platform_core import connectors, document_worker, graphs, jobs
from platform_core.document_worker import concurrency_from, fairly, start_lanes
from platform_core.graphs import claim_generation, reconcile_if_due, refresh_job
from platform_core.jobs import Kind, Later, claim, enqueue, run_next
from platform_core.models import (
    AIConfiguration,
    ApplicationFeature,
    Connector,
    Document,
    Job,
    KnowledgeEntry,
    KnowledgeGraph,
)
from platform_core.workbench import add_knowledge

from . import test_documents

#: What the test kind's handler does next: None, "later", or an exception.
BEHAVIOUR = {"next": None}
CALLS = []


def handler(job):
    CALLS.append(job.subject)
    action = BEHAVIOUR["next"]
    if action == "later":
        raise Later(30)
    if isinstance(action, Exception):
        raise action


TEST_KINDS = {
    "test_once": Kind(lane="test", handler="platform_core.tests.test_jobs.handler"),
    "test_thrice": Kind(
        lane="test", handler="platform_core.tests.test_jobs.handler", max_attempts=3
    ),
    "test_slow": Kind(
        lane="test",
        handler="platform_core.tests.test_jobs.handler",
        delay=timedelta(seconds=60),
    ),
}


@patch.dict(jobs.KINDS, TEST_KINDS)
class JobQueueTests(TestCase):
    def setUp(self):
        BEHAVIOUR["next"] = None
        CALLS.clear()

    def test_a_burst_of_requests_is_one_waiting_job_due_at_the_earliest(self):
        enqueue("test_slow", "a")
        enqueue("test_slow", "a")
        enqueue("test_slow", "a", delay=timedelta(0))
        waiting = Job.objects.get(key="test_slow:a", status="queued")
        self.assertLessEqual(waiting.run_after, timezone.now())
        self.assertEqual(Job.objects.count(), 1)

    def test_a_change_that_rolls_back_takes_its_job_with_it(self):
        with self.assertRaises(RuntimeError), transaction.atomic():
            enqueue("test_once", "a")
            raise RuntimeError
        self.assertFalse(Job.objects.exists())

    def test_nothing_is_claimed_before_it_is_due(self):
        enqueue("test_slow", "a")
        self.assertIsNone(claim("test"))
        self.assertIsNotNone(claim("test", now=timezone.now() + timedelta(minutes=2)))

    def test_one_key_never_runs_twice_at_once(self):
        enqueue("test_once", "a")
        first = claim("test")
        # A change while it runs: a row of its own, not folded into running work.
        enqueue("test_once", "a")
        enqueue("test_once", "b")
        self.assertEqual(Job.objects.filter(key="test_once:a").count(), 2)
        second = claim("test")
        self.assertEqual(second.subject, "b", "a's second request waits for the first")
        self.assertIsNone(claim("test"))
        Job.objects.filter(pk=first.pk).update(status="done")
        self.assertEqual(claim("test").subject, "a")

    def test_one_application_cannot_hold_every_worker(self):
        busy, quiet = (
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
        )
        for subject in ("1", "2", "3"):
            enqueue("test_once", subject, application_id=busy)
        enqueue("test_once", "4", application_id=quiet)
        Job.objects.filter(subject="4").update(run_after=timezone.now() + timedelta(seconds=1))
        later = timezone.now() + timedelta(seconds=2)
        taken = [claim("test", now=later).subject for _ in range(jobs.TENANT_CAP)]
        self.assertEqual(taken, ["1", "2"])
        self.assertEqual(claim("test", now=later).subject, "4", "the cap passes over it")
        self.assertIsNone(claim("test", now=later))

    def test_a_finished_job_is_done(self):
        enqueue("test_once", "a")
        self.assertTrue(run_next("test"))
        job = Job.objects.get()
        self.assertEqual((job.status, job.attempts), ("done", 1))
        self.assertIsNotNone(job.finished_at)
        self.assertEqual(job.lease_owner, "")
        self.assertFalse(run_next("test"))

    def test_not_yet_waits_without_using_an_attempt(self):
        enqueue("test_once", "a")
        BEHAVIOUR["next"] = "later"
        run_next("test")
        job = Job.objects.get()
        self.assertEqual((job.status, job.attempts), ("queued", 0))
        self.assertGreater(job.run_after, timezone.now() + timedelta(seconds=20))

    def test_a_single_attempt_kind_is_dead_after_one_failure(self):
        enqueue("test_once", "a")
        BEHAVIOUR["next"] = ValueError("no")
        with self.assertLogs("platform_core.jobs", "WARNING") as logs:
            run_next("test")
        job = Job.objects.get()
        self.assertEqual((job.status, job.last_error_type), ("dead", "ValueError"))
        self.assertNotIn("no", logs.records[0].getMessage())
        self.assertEqual(logs.records[0].exception_type, "ValueError")

    def test_a_retrying_kind_backs_off_then_gives_up(self):
        enqueue("test_thrice", "a")
        BEHAVIOUR["next"] = OSError()
        later = timezone.now()
        for attempt in (1, 2):
            with self.assertLogs("platform_core.jobs", "WARNING"):
                run_next("test")
            job = Job.objects.get()
            self.assertEqual((job.status, job.attempts), ("queued", attempt))
            wait = jobs.BACKOFF_SECONDS * 2 ** (attempt - 1)
            self.assertGreater(job.run_after, later + timedelta(seconds=wait - 5))
            Job.objects.update(run_after=timezone.now())
        with self.assertLogs("platform_core.jobs", "WARNING"):
            run_next("test")
        self.assertEqual(Job.objects.get().status, "dead")
        self.assertEqual(len(CALLS), 3)

    def test_a_job_put_back_folds_into_a_change_made_while_it_ran(self):
        enqueue("test_once", "a")
        job = claim("test")
        enqueue("test_once", "a")
        self.assertTrue(jobs.put_back(job, timezone.now(), used_attempt=False))
        self.assertEqual(Job.objects.get(pk=job.pk).status, "done")
        self.assertEqual(Job.objects.filter(key="test_once:a", status="queued").count(), 1)

    def test_an_unknown_kind_is_dead_not_retried(self):
        Job.objects.create(kind="gone", lane="test", key="gone:1", run_after=timezone.now())
        self.assertTrue(run_next("test"))
        job = Job.objects.get()
        self.assertEqual((job.status, job.last_error_type), ("dead", "UnknownKind"))
        self.assertEqual(CALLS, [])

    def test_a_job_whose_worker_went_away_is_put_back_or_dead(self):
        enqueue("test_thrice", "retry")
        enqueue("test_once", "once")
        claim("test")
        claim("test")
        Job.objects.update(lease_expires_at=timezone.now() - timedelta(seconds=1))
        with self.assertLogs("platform_core.jobs", "WARNING"):
            self.assertEqual(jobs.reclaim_expired(), 2)
        self.assertEqual(Job.objects.get(subject="retry").status, "queued")
        dead = Job.objects.get(subject="once")
        self.assertEqual((dead.status, dead.last_error_type), ("dead", "LeaseExpired"))

    def test_a_renewal_that_lands_first_keeps_the_job(self):
        enqueue("test_once", "a")
        job = claim("test")
        now = timezone.now()
        Job.objects.update(lease_expires_at=now + timedelta(minutes=1))
        self.assertEqual(jobs.reclaim_expired(now=now + timedelta(seconds=1)), 0)
        self.assertEqual(Job.objects.get(pk=job.pk).status, "running")

    def test_finished_jobs_are_purged_after_they_are_kept(self):
        now = timezone.now()
        for status, age in (("done", jobs.KEEP_DONE), ("dead", jobs.KEEP_DEAD)):
            for extra in (timedelta(days=1), -timedelta(days=1)):
                Job.objects.create(
                    kind="test_once",
                    lane="test",
                    key=f"{status}{extra}",
                    status=status,
                    run_after=now,
                    finished_at=now - age - extra,
                )
        self.assertEqual(jobs.purge_finished(), 2)
        self.assertEqual(Job.objects.count(), 2)

    def test_the_lane_step_is_named_for_its_lane(self):
        self.assertEqual(jobs.step_for("serviceops-tests").__name__, "run_serviceops_tests_job")


@override_settings(
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    DOCUMENT_AUTO_CONVERT=False,
)
class GraphTriggerTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)

    def source(self):
        return add_knowledge(
            self.owner, self.app.pk, "Configuration", "| Node | Queue |\n| --- | --- |\n| N1 | Q1 |"
        )

    def waiting(self, app=None):
        return Job.objects.filter(
            key=f"graph_refresh:{(app or self.app).pk}", status="queued"
        ).count()

    def settle(self):
        Job.objects.all().delete()

    def test_adding_a_source_asks_for_one_refresh_a_moment_later(self):
        self.source()
        self.source()
        job = Job.objects.get(status="queued")
        self.assertEqual(
            (job.kind, job.lane, job.application_id), ("graph_refresh", "graph", self.app.pk)
        )
        self.assertGreater(job.run_after, timezone.now(), "a burst is given time to finish")

    def test_queryset_writes_that_change_the_graph_ask_for_a_refresh(self):
        entry = self.source()
        for write in (
            lambda rows: rows.update(active=False),
            lambda rows: rows.update(sensitivity="restricted"),
            lambda rows: rows.update(digest="0" * 64),
            lambda rows: rows.delete(),
        ):
            self.settle()
            write(KnowledgeEntry.objects.filter(pk=entry.pk))
            self.assertEqual(self.waiting(), 1)

    def test_writes_that_cannot_change_the_graph_ask_for_nothing(self):
        entry = self.source()
        self.settle()
        KnowledgeEntry.objects.filter(pk=entry.pk).update(quality={})
        KnowledgeEntry.objects.filter(pk=entry.pk).update(document_type="")
        entry.save(update_fields=["quality"])
        KnowledgeEntry.objects.filter(application=self.other).update(active=False)
        self.assertFalse(Job.objects.exists())

    def test_a_saved_or_deleted_source_asks_for_a_refresh(self):
        entry = self.source()
        self.settle()
        entry.save()
        self.assertEqual(self.waiting(), 1)
        self.settle()
        entry.save(update_fields=["active"])
        self.assertEqual(self.waiting(), 1)

    def test_graph_settings_and_the_knowledge_switch_ask_for_a_refresh(self):
        rates = {"input_rate": Decimal("1"), "output_rate": Decimal("1")}
        AIConfiguration.objects.create(
            application=self.app, purpose="chat", provider="openai", **rates
        )
        self.assertFalse(Job.objects.exists(), "only graph generation is in the fingerprint")
        AIConfiguration.objects.create(
            application=self.app, purpose="graph_generation", provider="openai", **rates
        )
        self.assertEqual(self.waiting(), 1)
        self.settle()
        ApplicationFeature.objects.create(application=self.app, key="chat_api", enabled=False)
        self.assertFalse(Job.objects.exists())
        ApplicationFeature.objects.create(application=self.app, key="knowledge", enabled=False)
        self.assertEqual(self.waiting(), 1)

    def test_generate_asks_for_a_refresh_due_now(self):
        claimed, _ = claim_generation(self.owner, self.app)
        self.assertTrue(claimed)
        job = Job.objects.get(status="queued")
        self.assertLessEqual(job.run_after, timezone.now())

    def test_the_graph_lane_rebuilds_from_the_job(self):
        self.source()
        Job.objects.update(run_after=timezone.now())
        self.assertTrue(run_next("graph"))
        graph = KnowledgeGraph.objects.get(application=self.app)
        self.assertEqual(graph.status, "ready")
        self.assertEqual(graph.fingerprint, graphs.fingerprint(self.app.pk))
        self.assertEqual(Job.objects.get(kind="graph_refresh").status, "done")

    def test_a_refresh_waits_while_documents_are_converting(self):
        self.source()
        Document.objects.create(
            application=self.app,
            uploaded_by=self.owner,
            name="a.md",
            size=1,
            sha256="0" * 64,
            status="converting",
            origin="upload",
        )
        job = Job.objects.get()
        with self.assertRaises(Later):
            refresh_job(job)
        self.assertFalse(KnowledgeGraph.objects.filter(application=self.app).exists())

    def test_a_refresh_of_a_current_graph_does_nothing(self):
        self.source()
        graphs.rebuild(self.app.pk)
        revisions = KnowledgeGraph.objects.get(application=self.app).updated_at
        self.assertEqual(graphs.refresh_graph(self.app.pk), "current")
        self.assertEqual(KnowledgeGraph.objects.get(application=self.app).updated_at, revisions)

    def test_reconcile_asks_about_every_application_then_rests(self):
        self.source()
        KnowledgeGraph.objects.create(application=self.other, status="ready")
        self.settle()
        with patch.object(graphs, "_last_reconciled", 0.0):
            reconcile_if_due()
            self.assertEqual(self.waiting(), 1)
            self.assertEqual(self.waiting(self.other), 1)
            self.settle()
            reconcile_if_due()
            self.assertFalse(Job.objects.exists(), "not again inside the interval")

    def test_the_scan_still_brings_everything_current(self):
        self.source()
        self.assertTrue(graphs.process_next_graph())
        self.assertFalse(graphs.process_next_graph())


@override_settings(
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class ConnectorJobTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)

    def connector(self, **fields):
        values = {
            "application": self.app,
            "kind": "jira",
            "name": f"Board {Connector.objects.count()}",
            "created_by": self.owner,
            "sync_interval_minutes": 15,
        }
        return Connector.objects.create(**(values | fields))

    def test_every_due_connector_gets_one_waiting_job(self):
        due = self.connector()
        recent = self.connector(last_attempt_at=timezone.now())
        self.connector(sync_interval_minutes=0)
        self.connector(enabled=False)
        connectors.schedule_due()
        connectors.schedule_due()
        self.assertEqual(
            list(Job.objects.values_list("subject", "status")), [(str(due.pk), "queued")]
        )
        self.assertNotEqual(str(recent.pk), Job.objects.get().subject)

    def test_the_job_imports_as_the_schedule_did(self):
        due = self.connector()
        connectors.schedule_due()
        with patch.object(connectors, "run_scheduled") as run:
            self.assertTrue(run_next("connectors"))
        self.assertEqual(run.call_args.args[0].pk, due.pk)
        self.assertEqual(Job.objects.get().status, "done")

    def test_a_connector_imported_by_hand_meanwhile_is_not_imported_again(self):
        due = self.connector()
        connectors.schedule_due()
        Connector.objects.filter(pk=due.pk).update(last_attempt_at=timezone.now())
        with patch.object(connectors, "run_scheduled") as run:
            run_next("connectors")
        run.assert_not_called()

    def test_a_connector_deleted_meanwhile_is_skipped(self):
        due = self.connector()
        connectors.schedule_due()
        due.delete()
        with patch.object(connectors, "run_scheduled") as run:
            run_next("connectors")
        run.assert_not_called()
        self.assertEqual(Job.objects.get().status, "done")


class FairIntakeTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)

    def document(self, app, status, started=None):
        return Document.objects.create(
            application=app,
            uploaded_by=self.owner,
            name="a.md",
            size=1,
            sha256="0" * 64,
            status=status,
            origin="upload",
            conversion_started_at=started,
        )

    def test_an_application_with_nothing_converting_goes_first(self):
        self.document(self.app, "converting", timezone.now())
        self.document(self.app, "queued")
        theirs = self.document(self.other, "queued")
        expired = timezone.now() - timedelta(minutes=5)
        waiting = Document.objects.filter(status="queued")
        self.assertEqual(fairly(waiting, "converting", expired), theirs)

    def test_when_everyone_is_busy_the_oldest_still_goes(self):
        self.document(self.app, "converting", timezone.now())
        oldest = self.document(self.app, "queued")
        expired = timezone.now() - timedelta(minutes=5)
        waiting = Document.objects.filter(status="queued")
        self.assertEqual(fairly(waiting, "converting", expired), oldest)

    def test_a_stalled_conversion_does_not_count_as_busy(self):
        self.document(self.app, "converting", timezone.now() - timedelta(minutes=10))
        mine = self.document(self.app, "queued")
        self.document(self.other, "queued")
        expired = timezone.now() - timedelta(minutes=5)
        waiting = Document.objects.filter(status="queued")
        self.assertEqual(fairly(waiting, "converting", expired), mine)


class ConcurrencyTests(SimpleTestCase):
    def test_shared_lanes_may_run_several_threads(self):
        self.assertEqual(concurrency_from("intake=4, graph=2"), {"intake": 4, "graph": 2})
        self.assertEqual(concurrency_from(""), {})

    def test_a_lane_that_runs_one_at_a_time_refuses_more(self):
        for text in ("factory=2", "bogus=2", "intake=0", "intake=many", "intake=99"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                concurrency_from(text)
        self.assertEqual(concurrency_from("factory=1"), {"factory": 1})

    def test_each_thread_has_its_own_keeper(self):
        with patch.object(document_worker, "Thread") as thread:
            threads = start_lanes(
                [lane for lane in document_worker.LANES if lane[0] in {"intake", "factory"}],
                None,
                {"intake": 3},
            )
        self.assertEqual(len(threads), 4)
        names = [call.kwargs["name"] for call in thread.call_args_list]
        self.assertEqual(
            names, ["worker-intake-1", "worker-intake-2", "worker-intake-3", "worker-factory"]
        )
        keepers = {id(call.kwargs["args"][4]) for call in thread.call_args_list}
        self.assertEqual(len(keepers), 4)

    def test_only_lanes_that_claim_row_by_row_are_shared(self):
        self.assertEqual(document_worker.SHARED_LANES, {"intake", "graph", "connectors"})
        self.assertFalse(document_worker.LaneKeeper("intake").exclusive)
        self.assertTrue(document_worker.LaneKeeper("factory").exclusive)
