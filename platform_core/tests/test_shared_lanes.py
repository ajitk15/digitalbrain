"""Every lane but maintenance runs in every process (Phase 5, F3).

A lane was held by one process because some of its steps picked a row and then
acted on it, or found abandoned work by its age. These pin what makes sharing
safe: each step's claim, a race with a second worker lost cleanly, and work
found by age kept fresh while it runs. The race is staged by letting "another
worker" claim the row in the instant after this one read it.
"""

import time
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.core.exceptions import ValidationError
from django.db.models import QuerySet
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from platform_core import code_factory_build, keepalive
from platform_core.code_factory_build import prepare, process_next_checks, process_next_preparation
from platform_core.code_graph_ingest import process_next_head, process_next_repository
from platform_core.models import CodeRepository, FactoryRun

from .test_code_factory_delivery import SETTINGS, DeliveryGateTests


@contextmanager
def another_worker_claims(model, **fields):
    """The instant after a step reads a `model` row, another worker writes `fields` to it."""
    real = QuerySet.first

    def first(queryset):
        found = real(queryset)
        if found is not None and queryset.model is model:
            model.objects.filter(pk=found.pk).update(**fields)
        return found

    with patch.object(QuerySet, "first", first):
        yield


class KeepAliveTests(SimpleTestCase):
    def test_it_touches_the_row_while_the_work_runs_and_never_after(self):
        rows = MagicMock()
        rows.model.__name__ = "CodeReview"
        with patch.object(keepalive, "connection"), keepalive.KeepAlive(rows, every=0.01):
            deadline = time.monotonic() + 2
            while rows.update.call_count < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
        touched = rows.update.call_count
        self.assertGreaterEqual(touched, 2)
        self.assertEqual(set(rows.update.call_args.kwargs), {"updated_at"})
        time.sleep(0.05)
        self.assertEqual(rows.update.call_count, touched)

    def test_short_work_never_writes_from_a_second_thread(self):
        rows = MagicMock()
        with patch.object(keepalive, "connection"), keepalive.KeepAlive(rows, every=60):
            pass
        rows.update.assert_not_called()

    def test_a_failed_touch_is_logged_and_never_raised(self):
        rows = MagicMock()
        rows.model.__name__ = "CodeRepository"
        rows.update.side_effect = RuntimeError("database gone")
        with (
            patch.object(keepalive, "connection"),
            self.assertLogs("platform_core.keepalive", "WARNING") as logged,
            keepalive.KeepAlive(rows, every=0.01),
        ):
            deadline = time.monotonic() + 2
            while not logged.records and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertEqual(logged.records[0].event, "keepalive_failed")


@override_settings(**SETTINGS)
class PreparationRaceTests(TestCase):
    def setUp(self):
        DeliveryGateTests.setUp(self)
        FactoryRun.objects.filter(pk=self.run.pk).update(
            status="prepare_queued", acting_user=self.owner
        )
        credential = patch("platform_core.code_factory_build.write_credential", return_value="tok")
        credential.start()
        self.addCleanup(credential.stop)

    def test_losing_the_claim_leaves_the_other_workers_run_alone(self):
        with (
            another_worker_claims(FactoryRun, status="preparing"),
            patch.object(code_factory_build, "run_work_order") as agents,
        ):
            self.assertTrue(process_next_preparation())
        agents.assert_not_called()
        self.run.refresh_from_db()
        # It used to be marked failed here, under the worker still running it.
        self.assertEqual((self.run.status, self.run.error), ("preparing", ""))

    def test_the_lane_never_prepares_a_run_that_is_no_longer_queued(self):
        for status in ("prepared", "failed", "awaiting_review"):
            FactoryRun.objects.filter(pk=self.run.pk).update(status=status)
            with self.subTest(status=status), patch.object(code_factory_build, "run_work_order"):
                with self.assertRaisesMessage(ValidationError, "not ready to be implemented"):
                    prepare(self.owner, self.app.pk, self.run.pk, queued=True)
                self.run.refresh_from_db()
                self.assertEqual(self.run.status, status)

    def test_a_refusal_before_the_claim_still_fails_the_queued_run(self):
        from platform_core.models import ApplicationGrant

        ApplicationGrant.objects.filter(application=self.app, user=self.owner).update(
            can_approve=False
        )
        self.assertTrue(process_next_preparation())
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "failed")


@override_settings(**SETTINGS)
class PollClaimTests(TestCase):
    def setUp(self):
        DeliveryGateTests.setUp(self)
        FactoryRun.objects.filter(pk=self.run.pk).update(
            status="delivered", branch="digital-brain/ops-1", checks=[]
        )
        self.repository = CodeRepository.objects.create(
            application=self.app, added_by=self.owner, external_id="acme/widgets",
            name="acme/widgets", source_url="https://github.com/acme/widgets", status="ready",
        )

    def test_one_worker_reads_a_runs_checks_and_moves_its_turn_on_first(self):
        seen = []

        def refresh(run):
            seen.append(FactoryRun.objects.get(pk=run.pk).checks_read_at)
            return True

        with patch.object(code_factory_build, "refresh_checks", side_effect=refresh):
            self.assertTrue(process_next_checks())
        self.assertIsNotNone(seen[0])

    def test_a_second_worker_does_not_ask_github_about_the_same_run(self):
        with (
            another_worker_claims(FactoryRun, checks_read_at=timezone.now()),
            patch.object(code_factory_build, "refresh_checks") as refresh,
        ):
            self.assertTrue(process_next_checks())
        refresh.assert_not_called()

    def test_a_second_worker_does_not_ask_github_about_the_same_branch(self):
        with (
            another_worker_claims(CodeRepository, head_checked_at=timezone.now()),
            patch("platform_core.code_graph_ingest.refresh_head") as refresh,
        ):
            self.assertTrue(process_next_head())
        refresh.assert_not_called()


@override_settings(**SETTINGS)
class IndexKeepAliveTests(TestCase):
    def setUp(self):
        DeliveryGateTests.setUp(self)
        self.repository = CodeRepository.objects.create(
            application=self.app, added_by=self.owner, external_id="acme/widgets",
            name="acme/widgets", source_url="https://github.com/acme/widgets", status="queued",
        )

    def test_indexing_is_kept_fresh_and_only_once_it_is_claimed(self):
        held = {}

        class Recording:
            def __init__(self, rows):
                held["rows"] = rows

            def __enter__(self):
                held["before"] = held["rows"].count()
                return self

            def __exit__(self, *exc):
                return False

        def index(repository):
            CodeRepository.objects.filter(pk=repository.pk).update(status="indexing")
            held["during"] = held["rows"].count()
            return True

        with (
            patch.object(keepalive, "KeepAlive", Recording),
            patch("platform_core.code_graph_ingest.index_repository", side_effect=index),
        ):
            self.assertTrue(process_next_repository())
        self.assertEqual((held["before"], held["during"]), (0, 1))

    def test_a_fresh_index_is_not_taken_for_an_abandoned_one(self):
        CodeRepository.objects.filter(pk=self.repository.pk).update(
            status="indexing", updated_at=timezone.now() - timedelta(minutes=2)
        )
        with patch("platform_core.code_graph_ingest.index_repository") as index:
            self.assertFalse(process_next_repository())
        index.assert_not_called()
        self.repository.refresh_from_db()
        self.assertEqual(self.repository.status, "indexing")
