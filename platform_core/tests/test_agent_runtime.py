from django.test import SimpleTestCase

from platform_core.agent_runtime.runtime import (
    HistoryTurn,
    evidence_payload,
    recent_turns,
)
from platform_core.agent_runtime.usage import (
    ANSWER_LIMIT,
    checked_answer,
    checked_count,
    checked_request_id,
    claude_usage,
    openai_usage,
)


class UsageValidationTests(SimpleTestCase):
    def test_counts_reject_anything_that_is_not_a_bounded_integer(self):
        for value in (None, "10", 10.0, True, -1, 1000001, [10]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                checked_count(value)
        self.assertEqual(checked_count(0), 0)
        self.assertEqual(checked_count(1000000), 1000000)

    def test_missing_openai_usage_is_an_error_not_a_free_request(self):
        for raw in (None, [], "usage", {}, {"prompt_tokens": 5}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                openai_usage(raw, responses_api=False)

    def test_openai_responses_usage_is_renamed_not_dropped(self):
        self.assertEqual(
            openai_usage({"input_tokens": 12, "output_tokens": 3}, responses_api=True), (12, 3)
        )
        # The Responses field names must not satisfy the Chat Completions path.
        with self.assertRaises(ValueError):
            openai_usage({"input_tokens": 12, "output_tokens": 3}, responses_api=False)

    def test_claude_cache_tokens_are_billed_as_input(self):
        self.assertEqual(
            claude_usage(
                {
                    "input_tokens": 10,
                    "output_tokens": 3,
                    "cache_read_input_tokens": 5,
                    "cache_creation_input_tokens": 2,
                }
            ),
            (17, 3),
        )
        # Absent cache keys are genuinely zero, unlike absent token counts.
        self.assertEqual(claude_usage({"input_tokens": 10, "output_tokens": 3}), (10, 3))

    def test_claude_rejects_malformed_cache_counts(self):
        with self.assertRaises(ValueError):
            claude_usage({"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": -4})

    def test_answers_and_request_ids_are_bounded(self):
        self.assertEqual(checked_answer("ok"), "ok")
        # A long, legitimate answer - an implementation phase writing whole
        # files - is accepted up to what Code Factory's largest output cap
        # (64,000 tokens, ~4 characters each) can produce.
        from platform_core.code_factory import MAX_OUTPUT_LIMIT

        self.assertTrue(checked_answer("x" * (MAX_OUTPUT_LIMIT * 4)))
        for value in (None, "", "   ", "x" * (ANSWER_LIMIT + 1)):
            with self.subTest(value=value), self.assertRaises(ValueError):
                checked_answer(value)
        self.assertEqual(checked_request_id("req-1"), "req-1")
        for value in (None, "", "x" * 161, 7):
            with self.subTest(value=value), self.assertRaises(ValueError):
                checked_request_id(value)


class HistoryAndEvidenceTests(SimpleTestCase):
    def test_history_is_capped_and_prior_answers_truncated(self):
        history = [HistoryTurn(question=f"q{i}", answer="a" * 5000) for i in range(12)]
        turns = recent_turns(history)
        self.assertEqual(len(turns), 8)
        self.assertEqual(turns[0].question, "q4")
        self.assertEqual(len(turns[0].answer), 4000)

    def test_history_accepts_any_object_exposing_question_and_answer(self):
        self.assertEqual(recent_turns(None), [])
        rows = recent_turns([HistoryTurn(question="q", answer="a")])
        self.assertEqual(rows, [HistoryTurn(question="q", answer="a")])

    def test_evidence_is_numbered_and_never_carries_the_digest(self):
        payload = evidence_payload(
            [{"id": "1", "title": "Policy", "excerpt": "Thirty days", "digest": "secret"}]
        )
        self.assertEqual(payload[0]["source"], 1)
        self.assertNotIn("digest", payload[0])
        self.assertEqual(evidence_payload([]), [])


class FailureLabelTests(SimpleTestCase):
    def test_a_rejected_answer_is_labelled_in_the_log_without_its_text(self):
        """The formatter keeps no exception message, so an answer thrown away by
        the length check and a stream with no result used to read identically."""
        import json
        from types import SimpleNamespace
        from unittest.mock import patch

        from django.core.exceptions import ValidationError

        from platform_core import claude_agents
        from platform_core.observability import SafeJsonFormatter

        config = SimpleNamespace(model="claude-sonnet-5")

        def rejected(coroutine):
            coroutine.close()
            raise ValueError("Invalid answer")

        with (
            patch.object(claude_agents.asyncio, "run", side_effect=rejected),
            self.assertLogs("platform_core.claude_agents", "WARNING") as logs,
            self.assertRaises(ValidationError),
        ):
            claude_agents.completion(config, "q", [], "token")
        record = next(r for r in logs.records if r.getMessage() == "claude_completion_failed")
        entry = json.loads(SafeJsonFormatter().format(record))
        self.assertEqual(entry["failure_kind"], "answer_rejected")
        self.assertNotIn("Invalid answer", json.dumps(entry))
