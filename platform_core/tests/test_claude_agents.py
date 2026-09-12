import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from claude_agent_sdk import ResultMessage
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from platform_core.claude_agents import completion


class ClaudeSDKTests(SimpleTestCase):
    def result(self, **kwargs):
        return ResultMessage(
            subtype="success",
            duration_ms=5,
            duration_api_ms=4,
            is_error=False,
            num_turns=1,
            session_id="test-session",
            result="A clear answer [1].",
            usage={"input_tokens": 10, "output_tokens": 4, "cache_read_input_tokens": 3},
            **kwargs,
        )

    def call(self):
        return completion(
            SimpleNamespace(model="claude-sonnet-5"),
            "Explain Alpha",
            [{"id": "source", "title": "Policy", "excerpt": "Alpha exists"}],
            "app-only-key",
            history=[SimpleNamespace(question="Alpha?", answer="It exists.")],
        )

    @patch.dict(
        "os.environ", {"ANTHROPIC_AUTH_TOKEN": "wrong-account", "OPENAI_API_KEY": "other-key"}
    )
    def test_scoped_key_context_disabled_tools_and_isolated_settings(self):
        captured = {}

        async def fake_query(*, prompt, options):
            captured.update(prompt=prompt, options=options)
            self.assertTrue(Path(options.cwd).is_dir())
            yield self.result()

        with patch("platform_core.claude_agents.query", side_effect=fake_query):
            result, answer = self.call()
        options = captured["options"]
        self.assertEqual(options.env["ANTHROPIC_API_KEY"], "app-only-key")
        self.assertEqual(options.env["ANTHROPIC_AUTH_TOKEN"], "")
        self.assertEqual(options.env["OPENAI_API_KEY"], "")
        self.assertEqual(options.tools, [])
        self.assertEqual(options.setting_sources, [])
        self.assertTrue(options.strict_mcp_config)
        self.assertEqual(options.mcp_servers, {})
        self.assertEqual(options.permission_mode, "dontAsk")
        self.assertIn("no-session-persistence", options.extra_args)
        self.assertFalse(Path(options.cwd).exists())
        self.assertEqual(json.loads(captured["prompt"])["conversation"][0]["question"], "Alpha?")
        self.assertEqual(result["usage"]["prompt_tokens"], 13)
        self.assertEqual(answer, "A clear answer [1].")

    def test_failed_or_missing_usage_returns_safe_error(self):
        for invalid in ["error", "usage", "answer"]:

            async def fake_query(*, prompt, options, invalid=invalid):
                result = self.result()
                if invalid == "error":
                    result.is_error = True
                if invalid == "usage":
                    result.usage = {}
                if invalid == "answer":
                    result.result = ""
                yield result

            with (
                self.subTest(invalid=invalid),
                patch("platform_core.claude_agents.query", side_effect=fake_query),
            ):
                with self.assertRaises(ValidationError):
                    self.call()
