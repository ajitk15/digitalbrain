"""The OpenAI streaming runtime, exercised against the real Agents SDK.

Only the socket is faked. `Runner.run_streamed`, `stream_events()`, the Chat
Completions stream handler that synthesizes Responses-shaped deltas, tool
dispatch and usage extraction all execute for real, which is the part worth
testing: those are the SDK internals this runtime depends on and cannot see.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import httpx2
from django.test import SimpleTestCase
from openai import DefaultAsyncHttpxClient

from platform_core.agent_runtime import openai_runtime
from platform_core.agent_runtime.tools import ToolScope


def sse(*chunks):
    """A Chat Completions stream, as the wire carries it."""
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    return body + "data: [DONE]\n\n"


def chunk(content=None, finish=None, usage=None, tool=None):
    delta = {}
    if content is not None:
        delta["content"] = content
    if tool is not None:
        delta["tool_calls"] = [tool]
    payload = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "configured-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if usage:
        payload["usage"] = usage
        payload["choices"] = []
    return payload


USAGE = {"prompt_tokens": 120, "completion_tokens": 18, "total_tokens": 138}


class StreamingRuntimeTests(SimpleTestCase):
    def setUp(self):
        self.scope = ToolScope(
            user_id="11111111-1111-1111-1111-111111111111",
            app_id="22222222-2222-2222-2222-222222222222",
        )
        self.events = []

    def emit(self, event, data):
        self.events.append((event, data))

    def transport(self, *responses):
        """Serve each queued SSE body in turn, recording the requests."""
        self.requests = []
        bodies = list(responses)

        def handler(request):
            self.requests.append(request)
            body = bodies.pop(0) if bodies else sse(chunk(content="done", finish="stop"))
            return httpx2.Response(
                200,
                content=body.encode(),
                headers={"content-type": "text/event-stream", "x-request-id": "req-stream"},
            )

        return patch(
            "platform_core.llm_agents._http_client",
            side_effect=lambda: DefaultAsyncHttpxClient(
                transport=httpx2.MockTransport(handler),
                follow_redirects=False,
                trust_env=False,
            ),
        )

    def failing_transport(self):
        def handler(request):
            return httpx2.Response(401, json={"error": {"message": "private-data leaked here"}})

        return patch(
            "platform_core.llm_agents._http_client",
            side_effect=lambda: DefaultAsyncHttpxClient(
                transport=httpx2.MockTransport(handler),
                follow_redirects=False,
                trust_env=False,
            ),
        )

    def invoke(self):
        return asyncio.run(
            openai_runtime.run(
                "configured-model",
                "q",
                [],
                "application-key",
                [],
                self.scope,
                SimpleNamespace(exhausted=False, calls=0, record=lambda r: None),
                self.emit,
                None,
            )
        )

    def run_runtime(self, *responses, recorder=None, cancel=None):
        recorder = recorder or SimpleNamespace(
            exhausted=False, calls=0, record=lambda result: None
        )
        with self.transport(*responses):
            return asyncio.run(
                openai_runtime.run(
                    "configured-model",
                    "Which servers run under NODE1?",
                    [{"id": "s1", "title": "Policy", "excerpt": "NODE1 hosts three servers."}],
                    "application-key",
                    [],
                    self.scope,
                    recorder,
                    self.emit,
                    cancel,
                )
            )

    def test_text_arrives_as_incremental_deltas_not_one_block(self):
        turns, answer = self.run_runtime(
            sse(
                chunk(content="NODE1 hosts "),
                chunk(content="three integration "),
                chunk(content="servers."),
                chunk(finish="stop"),
                chunk(usage=USAGE),
            )
        )
        deltas = [data["t"] for event, data in self.events if event == "delta"]
        self.assertEqual(deltas, ["NODE1 hosts ", "three integration ", "servers."])
        self.assertEqual(answer, "NODE1 hosts three integration servers.")
        self.assertEqual(len(turns), 1)

    def test_usage_is_read_from_the_provider_not_the_sdk_aggregate(self):
        turns, _ = self.run_runtime(
            sse(chunk(content="ok"), chunk(finish="stop"), chunk(usage=USAGE))
        )
        self.assertEqual(turns[0].prompt_tokens, 120)
        self.assertEqual(turns[0].completion_tokens, 18)
        self.assertEqual(turns[0].request_id, "req-stream")

    def test_the_request_carries_the_application_key_and_stores_nothing(self):
        self.run_runtime(sse(chunk(content="ok"), chunk(finish="stop"), chunk(usage=USAGE)))
        request = self.requests[0]
        self.assertEqual(request.headers["authorization"], "Bearer application-key")
        body = json.loads(request.content)
        self.assertFalse(body["store"])
        self.assertTrue(body["stream"])
        # The knowledge tools must actually reach the provider.
        self.assertEqual(
            sorted(t["function"]["name"] for t in body["tools"]),
            ["fetch_source", "search_knowledge"],
        )

    def test_a_tool_call_is_executed_and_reported_before_the_answer(self):
        calls = []

        class Recorder:
            exhausted = False
            calls = 0

            def record(self, result):
                calls.append(result)

        tool_chunk = {
            "index": 0,
            "id": "call_1",
            "type": "function",
            "function": {"name": "search_knowledge", "arguments": '{"query":"NODE1"}'},
        }
        with patch(
            "platform_core.agent_runtime.openai_runtime._call",
            return_value=asyncio.sleep(0, result="one matching passage"),
        ):
            turns, answer = self.run_runtime(
                sse(chunk(tool=tool_chunk), chunk(finish="tool_calls"), chunk(usage=USAGE)),
                sse(chunk(content="Three servers."), chunk(finish="stop"), chunk(usage=USAGE)),
                recorder=Recorder(),
            )
        tools = [data["name"] for event, data in self.events if event == "tool"]
        self.assertEqual(tools, ["search_knowledge"])
        self.assertEqual(answer, "Three servers.")
        # One receipt per model turn: the tool turn and the answering turn.
        self.assertEqual(len(turns), 2)

    def test_a_provider_failure_propagates_for_the_caller_to_sanitise(self):
        """The runtime is an internal boundary: it raises the provider's own error.

        Sanitising is ai.stream_chat_answer's job, and the test below pins that -
        splitting it this way keeps the provider's message available for logging
        while guaranteeing it never reaches a user.
        """
        from openai import AuthenticationError

        with self.failing_transport():
            with self.assertRaises(AuthenticationError):
                self.invoke()

    def test_the_caller_turns_that_into_a_message_with_no_provider_detail(self):
        from django.core.exceptions import ValidationError

        from platform_core import ai

        config = SimpleNamespace(provider="openai", model="configured-model")
        session = SimpleNamespace(emit=self.emit, cancel=None, stopped=False)
        app = SimpleNamespace(pk=self.scope.app_id)
        user = SimpleNamespace(pk=self.scope.user_id)
        with self.failing_transport(), patch.object(ai, "record_turns"), patch.object(ai, "access"):
            with self.assertRaises(ValidationError) as raised:
                ai.stream_chat_answer(user, app, config, "key", "q", [], [], session)
        message = " ".join(raised.exception.messages)
        self.assertNotIn("private-data", message)
        self.assertIn("AI response unavailable", message)

    def test_missing_usage_is_refused_rather_than_billed_as_zero(self):
        """A request whose usage we could not parse was still billed by the provider."""
        with self.assertRaises(ValueError):
            self.run_runtime(sse(chunk(content="ok"), chunk(finish="stop")))
