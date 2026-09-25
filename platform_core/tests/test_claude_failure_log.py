"""A failed Claude CLI result is diagnosable from the log, without its prose.

Three live change reviews failed as a bare "api_error" at about 175 seconds.
The SDK raises `ResultError` while the stream is read, and the module logged
only the exception type, so nothing said whether that was an overload, a
timeout or a request the API refused. The provider's own text is never written
anywhere - it may carry request details - so what is logged is its structured
fields and a fixed label for the kind of error.
"""

import json
import logging

from claude_agent_sdk import ResultError
from django.test import SimpleTestCase

from platform_core.claude_agents import log_result_failure
from platform_core.observability import SafeJsonFormatter


class ClaudeFailureLogTests(SimpleTestCase):
    def logged(self, failure):
        with self.assertLogs("platform_core.claude_agents", level="WARNING") as captured:
            log_result_failure(failure, max_tokens=4096)
        return json.loads(SafeJsonFormatter().format(captured.records[-1]))

    def failure(self, prose):
        return ResultError(
            "Claude run failed",
            data={
                "subtype": "success",
                "is_error": True,
                "terminal_reason": "api_error",
                "result": prose,
                "stop_reason": None,
                "duration_ms": 175000,
                "duration_api_ms": 174000,
                "num_turns": 1,
                "usage": {"output_tokens": 0},
            },
        )

    def test_the_structured_fields_and_a_label_are_logged(self):
        entry = self.logged(self.failure("API Error: Request timed out. patient 123"))
        self.assertEqual(entry["event"], "claude_result_error")
        self.assertEqual(entry["terminal_reason"], "api_error")
        self.assertEqual(entry["api_error_kind"], "timeout")
        self.assertEqual(entry["duration_api_ms"], 174000)
        self.assertEqual(entry["max_tokens"], 4096)

    def test_the_providers_text_is_never_written(self):
        entry = self.logged(self.failure("API Error: Overloaded. patient 123 secret"))
        self.assertEqual(entry["api_error_kind"], "overloaded")
        self.assertNotIn("patient 123", json.dumps(entry))

    def test_anything_else_is_quietly_skipped(self):
        with self.assertNoLogs("platform_core.claude_agents", level="WARNING"):
            log_result_failure(ValueError("No successful result"))
        self.assertTrue(logging.getLogger("platform_core.claude_agents"))
