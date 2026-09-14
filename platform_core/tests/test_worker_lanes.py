"""The background worker runs independent lanes rather than one shared loop.

The defect: links, conversions, graph runs and retention all ran in sequence on
one thread. A graph run may legitimately spend ten minutes waiting on a provider
- the extraction timeout is 600 seconds - and every document conversion and link
import across every application waited behind it.
"""

import threading
import time
from unittest.mock import patch

from django.test import SimpleTestCase

from platform_core import document_worker
from platform_core.document_worker import LANES, run_lane


class LaneCompositionTests(SimpleTestCase):
    def test_graph_work_is_not_on_the_same_lane_as_intake(self):
        """Sharing a lane is exactly what made a slow graph run block imports."""
        lanes = {name: steps_for() for name, steps_for, _ in LANES}
        intake = {step.__name__ for step in lanes["intake"]}
        graph = {step.__name__ for step in lanes["graph"]}
        self.assertIn("process_next_link", intake)
        self.assertIn("process_next_document", intake)
        self.assertIn("process_next_graph", graph)
        self.assertFalse(intake & graph)

    def test_every_step_belongs_to_exactly_one_lane(self):
        seen = [step.__name__ for _, steps_for, _ in LANES for step in steps_for()]
        self.assertEqual(len(seen), len(set(seen)))

    def test_the_slower_a_lane_the_longer_it_waits_when_idle(self):
        delays = {name: idle for name, _, idle in LANES}
        self.assertLess(delays["intake"], delays["maintenance"])


class Done(BaseException):
    """Ends a lane from inside a step.

    run_lane contains `Exception` so one bad step never kills its queue, so a
    test needs something outside that hierarchy to stop the loop - which also
    proves the containment is doing what it claims.
    """


class LaneBehaviourTests(SimpleTestCase):
    """These drive the real run_lane, not a copy of its loop."""

    def lane(self, name, step, idle):
        thread = threading.Thread(
            target=lambda: self.swallow(run_lane, name, lambda: (step,), idle), daemon=True
        )
        thread.start()
        return thread

    @staticmethod
    def swallow(function, *args):
        try:
            function(*args)
        except Done:
            pass

    def test_a_slow_lane_does_not_hold_up_a_fast_one(self):
        """The point of the split, measured rather than asserted."""
        graph_calls, intake_calls = [], []

        def slow_graph():
            graph_calls.append(1)
            if len(graph_calls) > 2:
                raise Done
            time.sleep(0.2)
            return True

        def quick_intake():
            intake_calls.append(1)
            if len(intake_calls) > 20:
                raise Done
            return False

        graph = self.lane("graph", slow_graph, 0.01)
        intake = self.lane("intake", quick_intake, 0.005)
        intake.join(timeout=3)
        graph.join(timeout=3)
        # Intake completed its whole run while the graph lane was still blocked.
        self.assertGreater(len(intake_calls), 10, "intake was starved by the slow lane")

    def test_a_failing_step_does_not_stop_its_lane(self):
        calls = []

        def failing():
            calls.append(1)
            if len(calls) > 3:
                raise Done
            raise RuntimeError("provider exploded")

        self.lane("graph", failing, 0.001).join(timeout=3)
        self.assertGreater(len(calls), 3, "the lane stopped after one failure")

    def test_a_lane_with_work_to_do_does_not_wait_between_items(self):
        """A backlog drains at the speed of the work, not one item per tick."""
        calls = []

        def busy():
            calls.append(time.monotonic())
            if len(calls) >= 5:
                raise Done
            return True

        started = time.monotonic()
        self.lane("intake", busy, 5.0).join(timeout=3)
        self.assertEqual(len(calls), 5)
        self.assertLess(time.monotonic() - started, 1.0, "it slept despite having work")


class WorkerStartupTests(SimpleTestCase):
    def test_every_lane_is_started_and_the_caller_runs_one(self):
        """One lane per thread, with the calling thread taking the first."""
        started, ran = [], []

        class FakeThread:
            def __init__(self, target=None, args=(), name="", daemon=False):
                started.append((name, args[0], daemon))

            def start(self):
                pass

        with patch.object(document_worker, "Thread", FakeThread):
            with patch.object(document_worker, "run_lane", lambda *args: ran.append(args[0])):
                document_worker.run_document_worker()

        names = [name for name, _, _ in started]
        self.assertEqual(len(started) + len(ran), len(LANES))
        self.assertEqual(ran, [LANES[0][0]], "the calling thread should run the first lane")
        self.assertEqual(names, [f"worker-{name}" for name, _, _ in LANES[1:]])
        self.assertTrue(all(daemon for _, _, daemon in started), "lanes must not block shutdown")

    def test_no_lane_is_left_unstarted(self):
        covered = set()
        with patch.object(document_worker, "Thread") as thread:
            with patch.object(document_worker, "run_lane", lambda *args: covered.add(args[0])):
                document_worker.run_document_worker()
        for call in thread.call_args_list:
            covered.add(call.kwargs["args"][0])
        self.assertEqual(covered, {name for name, _, _ in LANES})


class LaneLoggingTests(SimpleTestCase):
    def test_a_lane_failure_is_logged_with_an_event_and_the_lane_name(self):
        """The formatter is an allowlist: without `event` a failure logs as nothing."""

        def failing():
            raise RuntimeError("boom")

        with patch.object(document_worker, "logger") as logger:
            with patch.object(document_worker.time, "sleep", side_effect=StopIteration):
                with self.assertRaises(StopIteration):
                    run_lane("graph", lambda: (failing,), 0)
        extra = logger.warning.call_args.kwargs["extra"]
        self.assertEqual(extra["event"], "worker_lane_failed")
        self.assertEqual(extra["lane"], "graph")

    def test_the_formatter_keeps_the_lane_name(self):
        import json
        import logging

        from platform_core.observability import SafeJsonFormatter

        record = logging.LogRecord("w", logging.WARNING, "", 0, "ignored", None, None)
        record.event = "worker_lane_failed"
        record.lane = "graph"
        entry = json.loads(SafeJsonFormatter().format(record))
        self.assertEqual(entry["event"], "worker_lane_failed")
        self.assertEqual(entry["lane"], "graph")
        # The message and any traceback stay out of the log by design.
        self.assertNotIn("ignored", json.dumps(entry))
