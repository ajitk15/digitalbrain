"""Phase time limits: implementation writes the most, and gets the longest."""

from unittest.mock import patch

from django.test import SimpleTestCase

from platform_core import code_factory


class PhaseTimeoutTests(SimpleTestCase):
    def test_implementation_gets_longer_than_the_rest(self):
        self.assertEqual(code_factory.phase_timeout("implementation"), 900)
        self.assertEqual(code_factory.phase_timeout("analysis"), code_factory.PHASE_TIMEOUT)

    def test_the_call_is_given_its_phase_limit(self):
        with (
            patch("platform_core.ai.invoke_ai", return_value="ok") as invoke,
            patch.object(code_factory, "output_limit", return_value=1000),
        ):
            code_factory.ask(None, "app", "implementation", "i", "q", [], {})
        self.assertEqual(invoke.call_args.kwargs["timeout"], 900)

    def test_a_healthy_long_call_is_not_mistaken_for_a_dead_run(self):
        """Stall detection counts from the longest phase, or it would reclaim an
        implementation call that is still well within its limit."""
        self.assertGreaterEqual(code_factory.STALL_AFTER.total_seconds(), 900 * 3)
