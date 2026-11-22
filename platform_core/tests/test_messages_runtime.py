"""Claude agent runs through the Messages API (`agent_runtime.messages_runtime`).

The real `anthropic` SDK over a mocked HTTP transport, inside the real harness:
what each request carries, what the model is shown (the CLI path's own prompt,
tool names and schemas), one receipt per turn under the provider's message id,
and that a turn already billed keeps its receipt whatever fails after it.
"""

import json
import os
from decimal import Decimal
from unittest.mock import patch

from django.http import Http404
from django.test import TestCase, override_settings

from platform_core import claude_messages
from platform_core.agent_runtime import claude_runtime, harness, messages_runtime, openai_runtime
from platform_core.agent_runtime.harness import AgentFailure, run_agent
from platform_core.agent_runtime.prompts import TOOL_INSTRUCTIONS
from platform_core.agent_runtime.tools import SEARCH_KNOWLEDGE, ToolScope
from platform_core.models import (
    AgentStep,
    AIConfiguration,
    AIUsage,
    ApplicationGrant,
)
from platform_core.workbench import add_knowledge

from . import test_documents
from .test_claude_messages import KEY, Transport, sse


def turn(number, text="", tool=None, stop=None, cache=None):
    """One streamed assistant turn: text, or a call of `tool` = (name, args);
    `cache` adds the usage's cache counts."""
    events = [{"type": "message_start", "message": {
        "id": f"msg_{number:02d}", "type": "message", "role": "assistant",
        "model": "claude-sonnet-5", "content": [], "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": 100, "output_tokens": 1, **(cache or {})},
    }}]
    if text:
        events += [
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": text}},
            {"type": "content_block_stop", "index": 0},
        ]
    if tool:
        name, args = tool
        index = 1 if text else 0
        events += [
            {"type": "content_block_start", "index": index, "content_block": {
                "type": "tool_use", "id": f"toolu_{number:02d}", "name": name, "input": {},
            }},
            {"type": "content_block_delta", "index": index,
             "delta": {"type": "input_json_delta", "partial_json": json.dumps(args)}},
            {"type": "content_block_stop", "index": index},
        ]
    events += [
        {"type": "message_delta",
         "delta": {"stop_reason": stop or ("tool_use" if tool else "end_turn"),
                   "stop_sequence": None},
         "usage": {"output_tokens": 20}},
        {"type": "message_stop"},
    ]
    return 200, sse(events)


SEARCH = "mcp__brain__search_knowledge"


@override_settings(CLAUDE_MESSAGES_API=True, CLAUDE_MESSAGES_AGENTS=True)
class MessagesRuntimeTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.source = add_knowledge(
            self.owner, self.app.pk, "Refund policy", "Refund requests expire after thirty days."
        )
        self.config = AIConfiguration.objects.create(
            application=self.app, purpose="chat", provider="claude", model="claude-sonnet-5",
            input_rate=Decimal("3"), output_rate=Decimal("15"), configured_by=self.owner,
        )
        self.events = []

    def run_with(self, transport, turns=4, wrap_up=False, token=KEY, **kwargs):
        with patch.object(claude_messages, "_http_client", transport.client):
            return run_agent(
                user=self.owner, app=self.app, config=self.config, token=token,
                purpose="chat", scope=ToolScope(user_id=self.owner.pk, app_id=self.app.pk),
                question="How long do refunds take?", citations=[], history=[],
                emit=lambda event, data: self.events.append((event, data)),
                max_tokens=512, prompt_characters=200, turns=turns, wrap_up=wrap_up,
                **kwargs,
            )

    def bodies(self, transport):
        return [json.loads(request.content) for request in transport.requests]

    def test_an_api_key_runs_here_and_anything_else_through_the_cli(self):
        self.assertIs(harness.runtime_for("claude", KEY), messages_runtime)
        self.assertIs(harness.runtime_for("claude", "sk-ant-oat01-oauth"), claude_runtime)
        self.assertIs(harness.runtime_for("claude", ""), claude_runtime)
        self.assertIs(harness.runtime_for("openai", KEY), openai_runtime)
        with override_settings(CLAUDE_MESSAGES_AGENTS=False):
            self.assertIs(harness.runtime_for("claude", KEY), claude_runtime)
        with override_settings(CLAUDE_MESSAGES_API=False):
            self.assertIs(harness.runtime_for("claude", KEY), claude_runtime)

    def test_a_run_searches_answers_and_keeps_a_receipt_per_turn(self):
        transport = Transport(
            turn(1, tool=(SEARCH, {"query": "refund deadline"})),
            turn(2, text="Thirty days."),
        )
        planted = {"ANTHROPIC_API_KEY": "sk-ant-api03-operator",
                   "ANTHROPIC_CUSTOM_HEADERS": "X-Leak: operator"}
        with patch.dict(os.environ, planted):
            result = self.run_with(transport, message_id=None)
        self.assertEqual(result.answer, "Thirty days.")
        self.assertEqual({c["id"] for c in result.citations}, {str(self.source.pk)})
        first, second = self.bodies(transport)
        # Shown what the CLI path shows: its system prompt, its tool names and schemas.
        self.assertEqual(first["system"], TOOL_INSTRUCTIONS)
        description, schema = SEARCH_KNOWLEDGE.wire("claude")
        self.assertIn({"name": SEARCH, "description": description, "input_schema": schema},
                      first["tools"])
        self.assertEqual(json.loads(first["messages"][0]["content"])["question"],
                         "How long do refunds take?")
        # The tool's answer goes back under its call's id.
        self.assertEqual(second["messages"][1]["content"][0]["type"], "tool_use")
        reply = second["messages"][2]["content"][0]
        self.assertEqual((reply["type"], reply["tool_use_id"]), ("tool_result", "toolu_01"))
        self.assertIn("Refund policy", reply["content"])
        for request in transport.requests:
            self.assertEqual(request.headers["x-api-key"], KEY)
            self.assertNotIn("x-leak", request.headers)
        self.assertEqual(
            sorted(AIUsage.objects.values_list("request_id", flat=True)), ["msg_01", "msg_02"]
        )
        self.assertIn(("tool", {"name": "search_knowledge", "detail": "refund deadline"}),
                      self.events)
        self.assertIn(("delta", {"t": "Thirty days."}), self.events)

    def test_one_turn_is_sent_no_tools(self):
        transport = Transport(turn(1, text="From the evidence."))
        result = self.run_with(transport, turns=1)
        self.assertEqual(result.answer, "From the evidence.")
        self.assertNotIn("tools", self.bodies(transport)[0])

    def test_a_tool_the_run_was_not_given_is_refused_and_the_run_goes_on(self):
        transport = Transport(
            turn(1, tool=("mcp__brain__delete_everything", {"confirm": True})),
            turn(2, text="Done."),
        )
        result = self.run_with(transport)
        self.assertEqual(result.answer, "Done.")
        reply = self.bodies(transport)[1]["messages"][2]["content"][0]
        self.assertEqual(reply["content"], messages_runtime.NOT_GIVEN)
        self.assertFalse(AgentStep.objects.exists())

    def test_out_of_turns_every_turn_is_billed_and_the_turn_kept_back_answers(self):
        searches = [turn(n, tool=(SEARCH, {"query": "refund"})) for n in range(1, 5)]
        transport = Transport(*searches, turn(5, text="Thirty days, from what was read."))
        result = self.run_with(transport, wrap_up=True)
        self.assertEqual(result.answer, "Thirty days, from what was read.")
        self.assertEqual(AIUsage.objects.count(), 5)
        # The turn kept back is asked with no tools.
        self.assertNotIn("tools", self.bodies(transport)[-1])

    def test_a_turn_already_billed_keeps_its_receipt_when_the_next_one_fails(self):
        transport = Transport(
            turn(1, tool=(SEARCH, {"query": "refund"})),
            (500, json.dumps({"type": "error", "error": {"type": "api_error",
                                                         "message": "the prompt was secret"}})),
        )
        with self.assertRaises(AgentFailure) as failed:
            self.run_with(transport)
        self.assertNotIn("secret", str(failed.exception))
        self.assertEqual(list(AIUsage.objects.values_list("request_id", flat=True)), ["msg_01"])

    def test_access_ended_on_the_way_ends_the_run_and_keeps_its_bill(self):
        owner, app = self.owner, self.app

        class Revoking(Transport):
            def __call__(self, request):
                # The grant goes while the model answers; its tool call is refused.
                ApplicationGrant.objects.filter(application=app, user=owner).delete()
                return super().__call__(request)

        transport = Revoking(turn(1, tool=(SEARCH, {"query": "refund"})))
        with self.assertRaises(Http404):
            self.run_with(transport)
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(AIUsage.objects.count(), 1)


def markers(body):
    """Where a request asked the cache to keep what came before."""
    found = []
    system = body["system"]
    if isinstance(system, list):
        found += [("system", i) for i, block in enumerate(system) if "cache_control" in block]
    for number, message in enumerate(body["messages"]):
        content = message["content"]
        if isinstance(content, list):
            found += [(number, i) for i, block in enumerate(content)
                      if "cache_control" in block]
    return found


@override_settings(CLAUDE_MESSAGES_API=True, CLAUDE_MESSAGES_AGENTS=True,
                   CLAUDE_PROMPT_CACHING=True)
class PromptCachingTests(TestCase):
    """The same run with `claude_prompt_caching` on: what is marked, and what it costs."""

    # Borrowed, not inherited: inheriting would run every test above again.
    setUp = MessagesRuntimeTests.setUp
    run_with = MessagesRuntimeTests.run_with
    bodies = MessagesRuntimeTests.bodies

    def test_a_run_with_tools_marks_its_instructions_and_its_newest_message(self):
        transport = Transport(
            turn(1, tool=(SEARCH, {"query": "refund deadline"}),
                 cache={"cache_creation_input_tokens": 60}),
            turn(2, text="Thirty days.",
                 cache={"cache_read_input_tokens": 900, "cache_creation_input_tokens": 50}),
        )
        result = self.run_with(transport)
        self.assertEqual(result.answer, "Thirty days.")
        first, second = self.bodies(transport)
        # The system prompt is the same words, as a block marked for the cache;
        # the tools render before it, so the marker keeps them too.
        self.assertEqual(first["system"], [{
            "type": "text", "text": TOOL_INSTRUCTIONS, "cache_control": {"type": "ephemeral"},
        }])
        self.assertEqual(second["system"], first["system"])
        self.assertEqual(second["tools"], first["tools"])
        # The marker moves to the newest message; the one before is sent as it was.
        self.assertEqual(markers(first), [("system", 0), (0, 0)])
        self.assertEqual(markers(second), [("system", 0), (2, 0)])
        self.assertEqual(second["messages"][0]["content"],
                         [{"type": "text", "text": first["messages"][0]["content"][0]["text"]}])
        self.assertEqual(json.loads(first["messages"][0]["content"][0]["text"])["question"],
                         "How long do refunds take?")
        self.assertEqual(second["messages"][2]["content"][0]["type"], "tool_result")
        # Each receipt says which prompt tokens the cache wrote and read, and is
        # priced by them: written at a quarter more, read at a tenth.
        receipts = {r.request_id: r for r in AIUsage.objects.all()}
        self.assertEqual(
            [(r.input_tokens, r.cache_read_tokens, r.cache_write_tokens)
             for r in (receipts["msg_01"], receipts["msg_02"])],
            [(160, 0, 60), (1050, 900, 50)],
        )
        # $3/M in, $15/M out: 100 plain + 900 read at 0.1 + 50 written at 1.25, 20 out.
        self.assertEqual(receipts["msg_02"].amount, Decimal("0.00105750"))

    def test_one_turn_marks_nothing(self):
        # Nothing would be read back: a write there would only cost more.
        transport = Transport(turn(1, text="From the evidence."))
        self.run_with(transport, turns=1)
        body = self.bodies(transport)[0]
        self.assertEqual(body["system"], TOOL_INSTRUCTIONS)
        self.assertEqual(markers(body), [])

    def test_off_the_request_is_the_one_sent_before(self):
        transport = Transport(turn(1, tool=(SEARCH, {"query": "refund"})), turn(2, text="Done."))
        with override_settings(CLAUDE_PROMPT_CACHING=False):
            self.run_with(transport)
        for body in self.bodies(transport):
            self.assertEqual(body["system"], TOOL_INSTRUCTIONS)
            self.assertEqual(markers(body), [])
