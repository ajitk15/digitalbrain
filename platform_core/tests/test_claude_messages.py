"""One-shot Claude calls with an API key: the real SDK over a mocked HTTP transport.

What matters here is what leaves the server - one request, to the one host,
carrying this application's key and nothing from the environment - and that
what comes back is accounted for exactly as the CLI path accounts for it.
"""

import json
import os
from decimal import Decimal
from unittest.mock import patch

import anthropic
import httpx2
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings

from platform_core import claude_messages
from platform_core.ai import invoke_ai
from platform_core.claude_messages import completion, uses_messages_api
from platform_core.models import AIConfiguration, AIUsage
from platform_core.workbench import add_knowledge

from . import test_documents

KEY = "sk-ant-api03-application-key"


def sse(events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)


def reply(text="Thirty days.", usage=None):
    usage = {"input_tokens": 120, "output_tokens": 1, "cache_read_input_tokens": 30} \
        if usage is None else usage
    return sse([
        {"type": "message_start", "message": {
            "id": "msg_01test", "type": "message", "role": "assistant",
            "model": "claude-sonnet-5", "content": [], "stop_reason": None,
            "stop_sequence": None, "usage": usage,
        }},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
         "usage": {"output_tokens": 7}},
        {"type": "message_stop"},
    ])


class Transport:
    """Records every request and answers each with the next canned response."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        status, body = self.responses.pop(0)
        return httpx2.Response(
            status,
            content=body.encode(),
            headers={"content-type": "text/event-stream" if status == 200 else "application/json"},
        )

    def client(self, timeout):
        return anthropic.DefaultHttpxClient(
            transport=httpx2.MockTransport(self), follow_redirects=False, trust_env=False
        )


class Config:
    model = "claude-sonnet-5"


class MessagesAdapterTests(SimpleTestCase):
    def call(self, transport, **options):
        with patch.object(claude_messages, "_http_client", transport.client):
            return completion(Config(), "Refund deadline?", [], KEY, **options)

    def test_one_request_with_this_applications_key_and_nothing_from_the_environment(self):
        transport = Transport((200, reply()))
        planted = {
            "ANTHROPIC_API_KEY": "sk-ant-api03-operator",
            "ANTHROPIC_AUTH_TOKEN": "operator-token",
            "ANTHROPIC_BASE_URL": "https://attacker.example",
            "ANTHROPIC_CUSTOM_HEADERS": "X-Leak: operator\nAuthorization: Bearer operator",
        }
        with patch.dict(os.environ, planted):
            result, answer = self.call(transport, instructions="Be brief.", max_tokens=64)
        self.assertEqual(len(transport.requests), 1)
        request = transport.requests[0]
        self.assertEqual(str(request.url), "https://api.anthropic.com/v1/messages")
        self.assertEqual(request.headers["x-api-key"], KEY)
        self.assertNotIn("authorization", request.headers)
        self.assertNotIn("x-leak", request.headers)
        body = json.loads(request.content)
        self.assertEqual(body["model"], "claude-sonnet-5")
        self.assertEqual(body["system"], "Be brief.")
        self.assertEqual(body["max_tokens"], 64)
        self.assertTrue(body["stream"])
        sent = json.loads(body["messages"][0]["content"])
        self.assertEqual(sent["question"], "Refund deadline?")
        self.assertEqual(answer, "Thirty days.")
        # The provider's own id keys the receipt; cache reads are billed input.
        self.assertEqual(result["id"], "msg_01test")
        self.assertEqual(result["usage"], {"prompt_tokens": 150, "completion_tokens": 7})

    def test_missing_usage_is_an_error_not_a_free_request(self):
        transport = Transport((200, reply(usage={"output_tokens": 1})))
        with self.assertRaises(ValidationError):
            self.call(transport)

    def test_an_empty_reply_is_refused(self):
        transport = Transport((200, reply(text="   ")))
        with self.assertRaises(ValidationError):
            self.call(transport)

    def test_a_refused_key_says_so_once_and_is_never_retried(self):
        error = json.dumps({"type": "error", "error": {"type": "authentication_error",
                                                        "message": "secret detail"}})
        transport = Transport((401, error), (200, reply()))
        with self.assertRaises(ValidationError) as raised:
            self.call(transport)
        self.assertEqual(len(transport.requests), 1)
        text = " ".join(raised.exception.messages)
        self.assertIn("rejected the API key", text)
        self.assertNotIn("secret detail", text)

    def test_an_overload_is_reported_with_its_status_only(self):
        error = json.dumps({"type": "error", "error": {"type": "overloaded_error",
                                                        "message": "details"}})
        transport = Transport((529, error))
        with self.assertRaises(ValidationError) as raised:
            self.call(transport)
        self.assertIn("overloaded", " ".join(raised.exception.messages))
        self.assertEqual(len(transport.requests), 1)


class ChoiceTests(SimpleTestCase):
    @override_settings(CLAUDE_MESSAGES_API=True)
    def test_only_an_api_key_goes_to_the_messages_api(self):
        self.assertTrue(uses_messages_api(KEY))
        self.assertFalse(uses_messages_api("sk-ant-oat01-subscription-token"))
        self.assertFalse(uses_messages_api(""), "host login is the CLI's stored session")
        self.assertFalse(uses_messages_api(None))

    @override_settings(CLAUDE_MESSAGES_API=False)
    def test_the_switch_sends_every_call_to_the_cli(self):
        self.assertFalse(uses_messages_api(KEY))


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    CLAUDE_MESSAGES_API=True,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class GatewayTests(TestCase):
    """Through `invoke_ai`: the gateway, the budget and the receipt, unchanged."""

    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.source = add_knowledge(
            self.owner, self.app.pk, "Policy", "Refunds are accepted for thirty days."
        )
        AIConfiguration.objects.create(
            application=self.app, purpose="chat", provider="claude", model="claude-sonnet-5",
            input_rate=Decimal("2"), output_rate=Decimal("10"), configured_by=self.owner,
        )
        self.citations = [
            {"id": str(self.source.pk), "title": "Policy", "excerpt": self.source.content}
        ]

    def test_an_api_key_is_answered_by_one_request_and_one_receipt(self):
        transport = Transport((200, reply()))
        with (
            patch("platform_core.secrets.application_secret", return_value=KEY),
            patch.object(claude_messages, "_http_client", transport.client),
            patch("platform_core.claude_agents.completion") as cli,
        ):
            answer = invoke_ai(self.owner, self.app.pk, "chat", "Refunds?", self.citations)
        cli.assert_not_called()
        self.assertEqual(answer, "Thirty days.")
        receipt = AIUsage.objects.get()
        self.assertEqual(receipt.request_id, "msg_01test")
        self.assertEqual((receipt.input_tokens, receipt.output_tokens), (150, 7))
        self.assertEqual(receipt.provider, "Anthropic")

    def test_an_oauth_token_still_goes_to_the_cli(self):
        with (
            patch("platform_core.secrets.application_secret",
                  return_value="sk-ant-oat01-subscription"),
            patch("platform_core.claude_messages._call") as messages,
            patch("platform_core.claude_agents.completion",
                  return_value=({"id": "claude-x", "usage": {"prompt_tokens": 1,
                                                             "completion_tokens": 1}}, "Yes.")),
        ):
            invoke_ai(self.owner, self.app.pk, "chat", "Refunds?", self.citations)
        messages.assert_not_called()
