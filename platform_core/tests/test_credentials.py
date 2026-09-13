from types import SimpleNamespace
from unittest.mock import patch

from claude_agent_sdk import ResultMessage
from django.test import SimpleTestCase, override_settings

from platform_core.agent_runtime.credentials import (
    claude_environment,
    describe,
    host_login_enabled,
    is_oauth_token,
)


class CredentialKindTests(SimpleTestCase):
    OAUTH = "sk-ant-oat01-example-token"
    API = "sk-ant-api03-example-key"

    def test_an_oauth_token_is_recognised(self):
        self.assertTrue(is_oauth_token(self.OAUTH))
        self.assertEqual(describe(self.OAUTH), "OAuth token")

    def test_an_api_key_is_the_default_reading(self):
        for value in [self.API, "some-other-credential"]:
            with self.subTest(value=value):
                self.assertFalse(is_oauth_token(value))
                self.assertEqual(describe(value), "API key")

    def test_no_credential_means_the_machine_login(self):
        self.assertEqual(claude_environment(""), {})
        self.assertEqual(claude_environment(None), {})
        self.assertIn("Claude Code login", describe(""))

    def test_each_credential_kind_goes_to_its_own_variable(self):
        self.assertEqual(
            claude_environment(self.OAUTH),
            {"CLAUDE_CODE_OAUTH_TOKEN": self.OAUTH, "ANTHROPIC_API_KEY": ""},
        )
        self.assertEqual(
            claude_environment(self.API),
            {"ANTHROPIC_API_KEY": self.API, "CLAUDE_CODE_OAUTH_TOKEN": ""},
        )

    def test_the_unused_variable_is_blanked_never_omitted(self):
        """A stale value must not survive into the subprocess and pick an account."""
        for value in (self.OAUTH, self.API):
            with self.subTest(value=value):
                env = claude_environment(value)
                self.assertIn("ANTHROPIC_API_KEY", env)
                self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", env)
                self.assertEqual(sum(1 for v in env.values() if v), 1)

    def test_surrounding_whitespace_is_ignored(self):
        self.assertEqual(
            claude_environment(f"  {self.OAUTH}\n")["CLAUDE_CODE_OAUTH_TOKEN"], self.OAUTH
        )


class ClaudeRunEnvironmentTests(SimpleTestCase):
    """The credential reaches the CLI in the right variable, sandbox intact."""

    def run_adapter(self, token):
        from platform_core import claude_agents

        captured = {}

        async def fake_query(prompt, options):
            captured["options"] = options
            yield ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="s1",
                usage={"input_tokens": 10, "output_tokens": 3},
                result="An answer.",
            )

        with patch.object(claude_agents, "query", fake_query):
            claude_agents.completion(
                SimpleNamespace(model="claude-sonnet-5"), "Question", [], token, history=None
            )
        return captured["options"].env

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "host-key"})
    def test_an_oauth_token_is_passed_as_an_oauth_token(self):
        env = self.run_adapter("sk-ant-oat01-application-token")
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "sk-ant-oat01-application-token")
        # A host API key must not survive to override it.
        self.assertEqual(env["ANTHROPIC_API_KEY"], "")

    @patch.dict("os.environ", {"CLAUDE_CODE_OAUTH_TOKEN": "host-token"})
    def test_an_api_key_is_passed_as_an_api_key(self):
        env = self.run_adapter("sk-ant-api03-application-key")
        self.assertEqual(env["ANTHROPIC_API_KEY"], "sk-ant-api03-application-key")
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "")

    def test_the_host_login_is_still_unreachable(self):
        """Whichever credential is mounted, the CLI gets an empty config home."""
        env = self.run_adapter("sk-ant-oat01-application-token")
        self.assertTrue(env["CLAUDE_CONFIG_DIR"])
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], env["HOME"])
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], env["USERPROFILE"])


class HostLoginTests(SimpleTestCase):
    """The development-only fallback to the operator's own Claude Code login."""

    def run_adapter(self, token):
        from platform_core import claude_agents

        captured = {}

        async def fake_query(prompt, options):
            captured["options"] = options
            yield ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="s1",
                usage={"input_tokens": 10, "output_tokens": 3},
                result="An answer.",
            )

        with patch.object(claude_agents, "query", fake_query):
            claude_agents.completion(
                SimpleNamespace(model="claude-sonnet-5"), "Question", [], token, history=None
            )
        return captured["options"].env

    @override_settings(CLAUDE_USE_HOST_LOGIN=False)
    def test_it_is_off_unless_configured(self):
        self.assertFalse(host_login_enabled())

    @override_settings(CLAUDE_USE_HOST_LOGIN=True)
    def test_when_on_the_real_config_home_is_left_visible(self):
        env = self.run_adapter("")
        # Absent from the override map means inherited, which is the whole point.
        for name in ("CLAUDE_CONFIG_DIR", "HOME", "USERPROFILE"):
            self.assertNotIn(name, env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)

    @override_settings(CLAUDE_USE_HOST_LOGIN=True)
    def test_a_mounted_credential_still_wins_and_still_sandboxes(self):
        """Host login is a fallback, never an override of a real credential."""
        env = self.run_adapter("sk-ant-api03-application-key")
        self.assertEqual(env["ANTHROPIC_API_KEY"], "sk-ant-api03-application-key")
        self.assertTrue(env["CLAUDE_CONFIG_DIR"])
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], env["HOME"])

    @override_settings(CLAUDE_USE_HOST_LOGIN=False)
    def test_without_the_setting_an_empty_credential_still_sandboxes(self):
        """A missing secret must not silently become 'use the operator's account'."""
        env = self.run_adapter("")
        self.assertTrue(env["CLAUDE_CONFIG_DIR"])
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], env["HOME"])
