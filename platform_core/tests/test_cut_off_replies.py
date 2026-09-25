"""A reply that ran out of output budget says so.

A live run's work order used 2,145 output tokens against a 2,048 limit. The
reply stopped mid-sentence, the JSON never closed, and the run said only "did
not return usable JSON" - which points at the model, when the cause was the
budget.
"""

import time

from django.test import TestCase, override_settings

from platform_core.code_factory import PHASE_TOKENS, finish_phase, start_phase

from . import test_code_factory_delivery as delivery


@override_settings(**delivery.SETTINGS)
class CutOffReplyTests(TestCase):
    def setUp(self):
        delivery.DeliveryGateTests.setUp(self)

    def failed(self, spent):
        phase = start_phase(self.run, "work_order")
        finish_phase(
            phase,
            "failed",
            time.monotonic(),
            error="Work order did not return usable JSON.",
            usage={"completion_tokens": spent, "prompt_tokens": 10},
        )
        phase.refresh_from_db()
        return phase.error

    def test_a_reply_at_its_limit_is_said_to_be_cut_off(self):
        error = self.failed(PHASE_TOKENS["work_order"])
        self.assertIn("most likely cut off", error)
        self.assertIn(f"{PHASE_TOKENS['work_order']:,}-token limit", error)

    def test_a_short_reply_is_not(self):
        self.assertEqual(self.failed(300), "Work order did not return usable JSON.")

    def test_the_short_json_phases_have_room_for_several_files(self):
        self.assertGreaterEqual(PHASE_TOKENS["work_order"], 4096)
        self.assertGreaterEqual(PHASE_TOKENS["review"], 4096)
