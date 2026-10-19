"""A maintenance step that has never run is due, however recently the host booted.

Each step remembers when it last ran as a `time.monotonic()` reading. That clock
counts from an unspecified point - on Linux, boot - so "never ran" written as 0.0
read as "ran a moment ago" for the first interval after a host started: a fresh
CI runner skipped every retention purge and SLO read, and so would a container on
a host that had just come up. "Never" is None.
"""

from unittest.mock import patch

from django.test import TestCase, override_settings

from platform_core import document_worker, graphs, jobs, queue_status, retention, slo

#: Ten seconds after boot: inside every interval these steps keep.
BOOTED = 10.0


class FreshHostTests(TestCase):
    def setUp(self):
        clock = patch("time.monotonic", return_value=BOOTED)
        clock.start()
        self.addCleanup(clock.stop)

    def test_retention_purges_on_its_first_step(self):
        retention._last = None
        with (
            override_settings(RETENTION={"audit_days": 2192}),
            patch.object(retention, "purge", return_value=1) as purge,
        ):
            self.assertTrue(retention.purge_if_due())
        purge.assert_called()

    def test_the_service_levels_are_read_on_the_first_step(self):
        slo._read = None
        with patch.object(slo, "measure", return_value=[]) as measure:
            slo.read_if_due()
        measure.assert_called_once()

    def test_the_queue_is_logged_on_the_first_step(self):
        queue_status._last_logged = None
        with patch.object(queue_status, "lane_status", return_value=[]) as lanes:
            queue_status.log_if_due()
        lanes.assert_called_once()

    def test_jobs_are_maintained_on_the_first_step(self):
        jobs._last_maintained = None
        with (
            patch.object(jobs, "reclaim_expired") as reclaim,
            patch.object(jobs, "purge_finished"),
        ):
            jobs.maintain()
        reclaim.assert_called_once()

    def test_chat_retention_runs_on_the_first_step(self):
        document_worker._last_purge = None
        with patch("platform_core.workbench.purge_expired_conversations") as purge:
            document_worker.purge_chat_if_due()
        purge.assert_called_once()

    def test_the_graph_reconcile_runs_on_the_first_step(self):
        with (
            patch.object(graphs, "_last_reconciled", None),
            patch.object(graphs, "candidate_applications", return_value=[]) as candidates,
        ):
            graphs.reconcile_if_due()
        candidates.assert_called_once()
