"""The agent harness (`agent_runtime.harness`), Phase 0 of the agentic AI plan.

Every guardrail is driven by a scripted model (`agent_runtime.scripted`) against a
real database, so no provider is needed and each case runs the code a real run
does: the registry, `run_tool`, the gateway's admission and receipts, the checks
after, and the trajectory.
"""

import json
import threading
from contextlib import contextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.test import SimpleTestCase, TestCase, override_settings

from platform_core import gateway
from platform_core.agent_runtime import claude_runtime, harness, openai_runtime, registry
from platform_core.agent_runtime.harness import AgentFailure, run_agent, tool_budget
from platform_core.agent_runtime.scripted import Script
from platform_core.agent_runtime.tools import (
    BUDGET_EXHAUSTED,
    CUT,
    FETCH_SOURCE,
    MAX_TOOL_CALLS,
    NOT_FOUND,
    RESULT_CHARACTERS,
    SEARCH_KNOWLEDGE,
    STOPPED,
    CitationRecorder,
    ToolResult,
    ToolScope,
    ToolSpec,
    run_tool,
    shape,
)
from platform_core.agent_runtime.usage import TurnLimit
from platform_core.models import (
    AgentStep,
    AIAdmission,
    AIBudget,
    AIConfiguration,
    AIUsage,
    ApplicationGrant,
    ChatConversation,
    ChatMessage,
    FeatureSwitch,
    KnowledgeEntry,
)
from platform_core.workbench import add_knowledge

from . import test_documents
from .test_metrics import Measured

GATEWAY = {
    "max_calls_per_application": 4,
    "max_calls_per_provider": 16,
    "slot_wait_seconds": 0,
    "admission_minutes": 15,
}


class RegistryTests(SimpleTestCase):
    def test_every_tool_is_named_once_and_serves_known_purposes(self):
        self.assertEqual(
            list(registry.TOOLS),
            ["search_knowledge", "fetch_source", "list_files", "read_file", "search_code",
             "find_symbol", "callers", "tests_for", "read_diff", "incident_context",
             "search_published_knowledge"],
        )
        for name, spec in registry.TOOLS.items():
            with self.subTest(tool=name):
                self.assertEqual(spec.name, name)
                self.assertTrue(spec.purposes)
                self.assertLessEqual(spec.purposes, set(gateway.PURPOSES))
                for provider in ("claude", "openai"):
                    description, schema = spec.wire(provider)
                    self.assertTrue(description)
                    self.assertEqual(schema["type"], "object")
                    self.assertTrue(set(schema["required"]) <= set(schema["properties"]))
                # OpenAI's strict mode needs every property required and no others.
                _, strict = spec.wire("openai")
                self.assertIs(strict["additionalProperties"], False)
                self.assertEqual(set(strict["required"]), set(strict["properties"]))
                self.assertIn(spec.detail[0], spec.schema["properties"])

    def test_a_scope_is_given_its_purposes_tools_and_no_others(self):
        chat = ToolScope(user_id="u", app_id="a", purpose="chat")
        self.assertEqual(registry.for_scope(chat), [SEARCH_KNOWLEDGE, FETCH_SOURCE])
        factory = ToolScope(user_id="u", app_id="a", purpose="plan_drafting")
        self.assertEqual(
            [spec.name for spec in registry.for_scope(factory)],
            ["list_files", "read_file", "search_code", "find_symbol", "callers", "tests_for"],
        )
        triage = ToolScope(user_id="u", app_id="a", purpose="serviceops_triage")
        self.assertEqual(
            [spec.name for spec in registry.for_scope(triage)],
            ["search_knowledge", "fetch_source", "incident_context", "search_published_knowledge"],
        )
        review = ToolScope(user_id="u", app_id="a", purpose="code_review")
        self.assertEqual(
            [spec.name for spec in registry.for_scope(review)],
            ["list_files", "read_file", "search_code", "find_symbol", "callers", "tests_for",
             "read_diff", "search_published_knowledge"],
        )
        docs = ToolScope(user_id="u", app_id="a", purpose="code_docs")
        self.assertEqual(
            [spec.name for spec in registry.for_scope(docs)],
            ["list_files", "read_file", "search_code", "find_symbol", "callers", "tests_for"],
        )
        review = ToolScope(user_id="u", app_id="a", purpose="document_review")
        self.assertEqual(registry.for_scope(review), [SEARCH_KNOWLEDGE, FETCH_SOURCE])
        for purpose in ("nonsense",):
            with self.subTest(purpose=purpose):
                self.assertEqual(registry.for_scope(ToolScope("u", "a", purpose)), [])

    def test_openai_is_shown_exactly_what_function_tool_derived_before(self):
        """Changing what a model reads is measured for drift first, never done in
        passing: the registry must reproduce the decorated functions it replaced."""
        from agents import function_tool

        @function_tool
        async def search_knowledge(query: str, limit: int = 5) -> str:
            """Search this application's knowledge sources for relevant passages.

            Args:
                query: What to look for.
                limit: Maximum passages to return, 1-8.
            """

        @function_tool
        async def fetch_source(source_id: str, offset: int = 0) -> str:
            """Read more of one knowledge source returned by search_knowledge.

            Args:
                source_id: The source id to read.
                offset: Character offset to start from.
            """

        scope = ToolScope(user_id="u", app_id="a")
        built = openai_runtime.build_tools(scope, None, lambda *a: None)
        for before, now in zip([search_knowledge, fetch_source], built, strict=True):
            with self.subTest(tool=before.name):
                self.assertEqual(now.name, before.name)
                self.assertEqual(now.description, before.description)
                self.assertEqual(
                    json.dumps(now.params_json_schema, sort_keys=True),
                    json.dumps(before.params_json_schema, sort_keys=True),
                )
                self.assertEqual(now.strict_json_schema, before.strict_json_schema)

    def test_claude_is_shown_exactly_the_schemas_it_was_before(self):
        scope = ToolScope(user_id="u", app_id="a")
        for spec, description in (
            (
                SEARCH_KNOWLEDGE,
                "Search this application's knowledge sources for passages relevant to a "
                "query. Returns numbered passages with their source ids.",
            ),
            (
                FETCH_SOURCE,
                "Read more of one knowledge source by its id, starting at a character "
                "offset. Use ids returned by search_knowledge.",
            ),
        ):
            with self.subTest(tool=spec.name):
                tool = claude_runtime.bound(spec, scope, None, lambda *a: None)
                self.assertEqual(tool.name, spec.name)
                self.assertEqual(tool.description, description)
                self.assertEqual(tool.input_schema, spec.schema)

    def test_openai_arguments_take_the_schemas_defaults_and_refuse_junk(self):
        self.assertEqual(
            openai_runtime.arguments(SEARCH_KNOWLEDGE, '{"query": "x"}'),
            {"query": "x", "limit": 5},
        )
        for raw in ("not json", "[1, 2]", None, '"text"'):
            with self.subTest(raw=raw):
                self.assertEqual(openai_runtime.arguments(FETCH_SOURCE, raw), {"offset": 0})


class ShapeTests(SimpleTestCase):
    def test_a_step_keeps_numbers_and_ids_and_never_text(self):
        source = "3f2b8f8e-4c55-4d2b-9a8e-2b6d1b0c9e11"
        self.assertEqual(
            shape(FETCH_SOURCE, {"source_id": source, "offset": 4000}),
            {"offset": 4000, "source_id": {"id": source}},
        )
        shaped = shape(
            SEARCH_KNOWLEDGE,
            {"query": "patient MRN 4471234", "limit": 3, "undeclared": "secret words"},
        )
        self.assertEqual(shaped, {"limit": 3, "query": {"characters": 19}})
        self.assertNotIn("4471234", json.dumps(shaped))
        self.assertNotIn("secret", json.dumps(shaped))
        # Not an id, so only its length; an enormous number is not kept either.
        self.assertEqual(
            shape(FETCH_SOURCE, {"source_id": "drop table", "offset": 10**12}),
            {"offset": {"characters": 13}, "source_id": {"characters": 10}},
        )

    def test_a_result_is_cut_at_the_bound_the_reservation_counts_on(self):
        long = ToolSpec(
            name="long",
            description="d",
            run=lambda scope, args: ToolResult("x" * (RESULT_CHARACTERS * 2)),
            schema={"type": "object", "properties": {}, "required": []},
        )
        recorder = CitationRecorder(ToolScope("u", "a"))
        text = run_tool(long, recorder.scope, {}, recorder)
        self.assertEqual(len(text), RESULT_CHARACTERS)
        self.assertTrue(text.endswith(CUT))
        self.assertEqual(recorder.steps[0]["result_characters"], RESULT_CHARACTERS)

    def test_the_tool_budget_shrinks_with_the_turns_the_budget_pays_for(self):
        self.assertEqual(tool_budget(1, 4), 0)
        self.assertEqual(tool_budget(4, 4), MAX_TOOL_CALLS)
        self.assertEqual(tool_budget(2, 4), 3)
        self.assertEqual(tool_budget(3, 4), 6)
        self.assertEqual(tool_budget(9, 4), MAX_TOOL_CALLS)


class TurnCostTests(SimpleTestCase):
    config = SimpleNamespace(input_rate=Decimal("3"), output_rate=Decimal("15"))

    def test_one_turn_is_the_single_call_reservation_it_always_was(self):
        self.assertEqual(
            gateway.turn_costs(self.config, 9000, 1024),
            [gateway.estimate(self.config, 9000, 1024)],
        )
        # Tool output never counts for a call that has no second turn.
        self.assertEqual(
            gateway.turn_costs(self.config, 9000, 1024, tool_characters=10**6),
            [gateway.estimate(self.config, 9000, 1024)],
        )

    def test_each_later_turn_rereads_the_prompt_replies_and_tool_results(self):
        costs = gateway.turn_costs(self.config, 9000, 1024, turns=3, tool_characters=6000)
        reply = 1024 * gateway.CHARACTERS_PER_TOKEN
        expected, total = [], Decimal("0")
        for turn, read in enumerate((9000, 9000 + 6000 + reply, 9000 + 6000 + 2 * reply)):
            total += gateway.estimate(self.config, read, 1024)
            expected.append(total)
            self.assertEqual(costs[turn], total)
        self.assertEqual(costs, expected)
        self.assertEqual(costs, sorted(costs))


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    AI_GATEWAY=GATEWAY,
)
class HarnessTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.source = add_knowledge(
            self.owner, self.app.pk, "Refund policy", "Refund requests expire after thirty days."
        )
        self.config = AIConfiguration.objects.create(
            application=self.app,
            purpose="chat",
            provider="claude",
            model="claude-sonnet-5",
            input_rate=Decimal("3"),
            output_rate=Decimal("15"),
            configured_by=self.owner,
        )
        self.events = []
        conversation = ChatConversation.objects.create(
            application=self.app, user=self.owner, title="t"
        )
        ChatMessage.objects.create(
            application=self.app, user=self.owner, conversation=conversation, role="user",
            body="How long do refunds take?", sequence=1,
        )
        self.message = ChatMessage.objects.create(
            application=self.app, user=self.owner, conversation=conversation,
            role="assistant", body="", status="streaming", sequence=2,
        )

    @contextmanager
    def scripted(self, script):
        with patch.object(harness, "runtime_for", return_value=script):
            yield script

    def run_script(
        self, *turns, cancel=None, user=None, message=True, turns_asked=4, wrap_up=False, then=()
    ):
        script = Script(*turns, then=then)
        with self.scripted(script):
            result = run_agent(
                user=user or self.owner,
                app=self.app,
                config=self.config,
                token="unused",
                purpose="chat",
                scope=ToolScope(user_id=(user or self.owner).pk, app_id=self.app.pk),
                question="How long do refunds take?",
                citations=[],
                history=[],
                emit=lambda event, data: self.events.append((event, data)),
                cancel=cancel,
                max_tokens=1024,
                prompt_characters=200,
                turns=turns_asked,
                message_id=self.message.pk if message else None,
                wrap_up=wrap_up,
            )
        return script, result

    def steps(self):
        return list(AgentStep.objects.filter(message=self.message).order_by("sequence"))

    def test_a_run_searches_reads_answers_and_leaves_a_receipt_a_turn(self):
        script, result = self.run_script(
            [("search_knowledge", {"query": "refund"})],
            [("fetch_source", {"source_id": str(self.source.pk)})],
            "Thirty days.",
        )
        self.assertEqual(result.answer, "Thirty days.")
        self.assertEqual(result.turns, 4)
        self.assertEqual(script.given, ["fetch_source", "search_knowledge"])
        self.assertEqual({c["id"] for c in result.citations}, {str(self.source.pk)})
        self.assertEqual(AIUsage.objects.filter(application=self.app, purpose="chat").count(), 3)
        self.assertFalse(AIAdmission.objects.exists())
        tools = [data["name"] for event, data in self.events if event == "tool"]
        self.assertEqual(tools, ["search_knowledge", "fetch_source"])
        steps = self.steps()
        self.assertEqual(
            [(s.sequence, s.tool, s.outcome) for s in steps],
            [(1, "search_knowledge", "found"), (2, "fetch_source", "found")],
        )
        self.assertEqual(steps[0].arguments, {"query": {"characters": 6}})
        self.assertEqual(steps[1].arguments, {"source_id": {"id": str(self.source.pk)}})
        self.assertGreater(steps[1].result_characters, 0)
        # A step says how the answer was reached, never what was read or asked.
        everything = json.dumps([s.arguments for s in steps])
        self.assertNotIn("refund", everything.lower())
        self.assertNotIn("thirty", everything.lower())

    def test_another_applications_source_reads_as_not_found(self):
        ApplicationGrant.objects.create(application=self.other, user=self.owner, role="owner")
        foreign = add_knowledge(
            self.owner, self.other.pk, "Secret policy", "The other refund deadline is secret."
        )
        script, result = self.run_script(
            [("fetch_source", {"source_id": str(foreign.pk)})],
            [("search_knowledge", {"query": "secret deadline"})],
            "Not found.",
        )
        self.assertEqual(script.tool_text[0], NOT_FOUND)
        # The search echoes its query; the other application's source is not in it.
        self.assertNotIn("Secret policy", script.tool_text[1])
        self.assertNotIn("other refund deadline", script.tool_text[1])
        self.assertEqual(result.citations, [])
        self.assertEqual([s.outcome for s in self.steps()], ["nothing", "nothing"])

    def test_a_restricted_source_is_never_read_or_cited(self):
        KnowledgeEntry.objects.filter(pk=self.source.pk).update(sensitivity="restricted")
        script, result = self.run_script(
            [("search_knowledge", {"query": "refund"})],
            [("fetch_source", {"source_id": str(self.source.pk)})],
            "Nothing to cite.",
        )
        self.assertNotIn("thirty", " ".join(script.tool_text).lower())
        self.assertEqual(script.tool_text[1], NOT_FOUND)
        self.assertEqual(result.citations, [])

    def test_a_grant_revoked_mid_run_ends_the_answer_and_keeps_the_receipts(self):
        def revoke():
            ApplicationGrant.objects.filter(application=self.app, user=self.owner).delete()

        with self.assertRaises(Http404):
            self.run_script(
                [("search_knowledge", {"query": "refund"}), revoke],
                [("search_knowledge", {"query": "refund"})],
                "An answer that must not be kept.",
            )
        # Three turns were billed; their receipts stay, the answer does not.
        self.assertEqual(AIUsage.objects.filter(application=self.app).count(), 3)
        self.assertEqual([s.outcome for s in self.steps()], ["found", "refused"])
        self.assertFalse(AIAdmission.objects.exists())

    def test_a_feature_switched_off_mid_run_ends_the_answer(self):
        def switch_off():
            FeatureSwitch.objects.create(key="knowledge", enabled=False)

        with self.assertRaises(PermissionDenied):
            self.run_script(
                [switch_off],
                [("search_knowledge", {"query": "refund"})],
                "An answer that must not be kept.",
            )
        self.assertEqual([s.outcome for s in self.steps()], ["refused"])

    def test_nothing_runs_for_someone_who_cannot_open_the_application(self):
        script = Script("never")
        with self.scripted(script), self.assertRaises(Http404):
            run_agent(
                user=self.admin, app=self.app, config=self.config, token="unused",
                purpose="chat", scope=ToolScope(user_id=self.admin.pk, app_id=self.app.pk),
                question="q", citations=[], history=[], emit=lambda *a: None,
                max_tokens=1024, prompt_characters=1,
            )
        self.assertIsNone(script.given)
        self.assertFalse(AIUsage.objects.exists())

    def test_stop_between_turns_runs_no_further_tool(self):
        cancel = threading.Event()
        script, result = self.run_script(
            [("search_knowledge", {"query": "refund"}), cancel.set],
            [("fetch_source", {"source_id": str(self.source.pk)})],
            "Stopped here.",
            cancel=cancel,
        )
        self.assertEqual(script.tool_text[1], STOPPED)
        self.assertEqual([s.outcome for s in self.steps()], ["found", "stopped"])
        self.assertEqual(self.steps()[1].result_characters, 0)
        self.assertEqual(result.answer, "Stopped here.")

    def test_the_tool_call_cap_tells_the_model_to_answer(self):
        searches = [("search_knowledge", {"query": "refund"})] * (MAX_TOOL_CALLS + 2)
        script, result = self.run_script(searches, "Enough.")
        self.assertEqual(script.tool_text[-1], BUDGET_EXHAUSTED)
        outcomes = [s.outcome for s in self.steps()]
        self.assertEqual(outcomes.count("found"), MAX_TOOL_CALLS)
        self.assertEqual(outcomes.count("budget"), 2)

    def test_a_tool_the_run_was_not_given_never_runs(self):
        script, _ = self.run_script(
            [("delete_everything", {"confirm": True}), ("search_knowledge", {"query": "x"})],
            "Done.",
        )
        self.assertEqual(script.seen, ["delete_everything", "search_knowledge"])
        self.assertEqual([s.tool for s in self.steps()], ["search_knowledge"])

    def test_a_scope_with_no_tools_is_one_turn_from_the_evidence(self):
        script = Script("From the evidence.")
        with self.scripted(script):
            result = run_agent(
                user=self.owner, app=self.app, config=self.config, token="unused",
                purpose="chat",
                scope=ToolScope(user_id=self.owner.pk, app_id=self.app.pk, purpose="nothing"),
                question="q", citations=[], history=[], emit=lambda *a: None,
                max_tokens=1024, prompt_characters=1,
            )
        self.assertEqual((script.given, script.max_turns, result.turns), ([], 1, 1))

    def test_every_turn_is_reserved_before_the_first_is_sent(self):
        AIBudget.objects.create(application=self.app, monthly_limit=Decimal("100"))
        held = []

        def look():
            held.append(AIAdmission.objects.get().reserved)

        self.run_script([look], "Answer.")
        costs = gateway.turn_costs(
            self.config, 200, 1024, turns=4, tool_characters=MAX_TOOL_CALLS * RESULT_CHARACTERS
        )
        self.assertEqual(held, [costs[-1]])
        self.assertGreater(costs[-1], 3 * costs[0])

    def test_a_budget_near_its_cap_pays_for_fewer_turns_and_fewer_tools(self):
        costs = gateway.turn_costs(
            self.config, 200, 1024, turns=4, tool_characters=MAX_TOOL_CALLS * RESULT_CHARACTERS
        )
        # Headroom for two turns and not three.
        AIBudget.objects.create(application=self.app, monthly_limit=(costs[1] + costs[2]) / 2)
        script, result = self.run_script(
            [("search_knowledge", {"query": "refund"})] * 4, "Answer."
        )
        self.assertEqual((result.turns, script.max_turns), (2, 2))
        self.assertEqual([s.outcome for s in self.steps()].count("found"), tool_budget(2, 4))

    def test_a_budget_with_room_for_one_turn_gets_no_tools(self):
        costs = gateway.turn_costs(
            self.config, 200, 1024, turns=4, tool_characters=MAX_TOOL_CALLS * RESULT_CHARACTERS
        )
        AIBudget.objects.create(application=self.app, monthly_limit=(costs[0] + costs[1]) / 2)
        script, result = self.run_script("From the evidence alone.")
        self.assertEqual((script.given, script.max_turns, result.turns), ([], 1, 1))

    def test_a_run_that_needs_more_turns_than_it_was_allowed_fails_and_keeps_its_bill(self):
        with self.assertRaises(AgentFailure) as failed:
            self.run_script(
                [("search_knowledge", {"query": "refund"})],
                [("search_knowledge", {"query": "refund"})],
                "Too late.",
                turns_asked=2,
            )
        self.assertEqual(failed.exception.kind, "TurnLimit")
        # What it did before failing is still kept, and both turns it sent are
        # billed: the receipts used to be lost with the answer.
        self.assertEqual(len(self.steps()), 2)
        self.assertEqual(AIUsage.objects.filter(application=self.app, purpose="chat").count(), 2)
        self.assertFalse(AIAdmission.objects.exists())

    def searches(self, count):
        return [[("search_knowledge", {"query": "refund"})] for _ in range(count)]

    def test_out_of_turns_a_wrap_up_answers_from_what_was_read(self):
        script, result = self.run_script(
            *self.searches(5), wrap_up=True, then=["Thirty days, from what was read."]
        )
        first, last = script.calls
        # The agent was given every turn it had before; one more was kept back.
        self.assertEqual(first["max_turns"], 4)
        self.assertEqual(first["given"], ["fetch_source", "search_knowledge"])
        self.assertEqual((last["given"], last["max_turns"]), ([], 1))
        self.assertEqual(last["question"], f"How long do refunds take?\n\n{harness.WRAP_UP}")
        # It answers over what the tools found that still verifies.
        self.assertEqual(last["evidence"], [str(self.source.pk)])
        self.assertEqual(result.answer, "Thirty days, from what was read.")
        self.assertEqual([c["id"] for c in result.citations], [str(self.source.pk)])
        # Four turns out of turns and the one kept back: five receipts.
        self.assertEqual(len(result.usages), 5)
        self.assertEqual(AIUsage.objects.filter(application=self.app, purpose="chat").count(), 5)
        self.assertEqual([s.outcome for s in self.steps()], ["found"] * 4)
        self.assertFalse(AIAdmission.objects.exists())

    def test_a_wrap_up_changes_nothing_for_an_answer_inside_its_turns(self):
        searches = [("search_knowledge", {"query": "refund"})] * (MAX_TOOL_CALLS + 2)
        script, result = self.run_script(searches, "Enough.", wrap_up=True, then=["never"])
        self.assertEqual((len(script.calls), script.max_turns, result.answer), (1, 4, "Enough."))
        # The same tool budget as a run of four turns without one.
        self.assertEqual([s.outcome for s in self.steps()].count("found"), MAX_TOOL_CALLS)
        self.assertEqual(AIUsage.objects.filter(application=self.app, purpose="chat").count(), 2)

    def test_a_wrap_up_is_reserved_with_the_turns(self):
        AIBudget.objects.create(application=self.app, monthly_limit=Decimal("100"))
        held = []

        def look():
            held.append(AIAdmission.objects.get().reserved)

        self.run_script([look], "Answer.", wrap_up=True)
        costs = gateway.turn_costs(
            self.config, 200, 1024, turns=5, tool_characters=MAX_TOOL_CALLS * RESULT_CHARACTERS
        )
        # The turn kept back is paid for before the first is sent, like every other.
        self.assertEqual(held, [costs[-1]])

    def test_with_no_turn_to_spare_out_of_turns_fails_as_before(self):
        costs = gateway.turn_costs(
            self.config, 200, 1024, turns=5, tool_characters=MAX_TOOL_CALLS * RESULT_CHARACTERS
        )
        # Headroom for two turns: a tool turn and its answer, nothing to keep back.
        AIBudget.objects.create(application=self.app, monthly_limit=(costs[1] + costs[2]) / 2)
        with self.assertRaises(AgentFailure) as failed:
            self.run_script(*self.searches(3), wrap_up=True, then=["never"])
        self.assertEqual(failed.exception.kind, "TurnLimit")
        self.assertEqual(AIUsage.objects.filter(application=self.app, purpose="chat").count(), 2)

    def test_access_ended_on_the_way_is_the_refusal_not_a_wrap_up(self):
        def revoke():
            ApplicationGrant.objects.filter(application=self.app, user=self.owner).delete()

        with self.assertRaises(Http404):
            self.run_script(
                [("search_knowledge", {"query": "refund"}), revoke], *self.searches(4),
                wrap_up=True, then=["An answer that must not be asked for."],
            )
        # The four turns sent are billed, and nothing more was asked.
        self.assertEqual(AIUsage.objects.filter(application=self.app).count(), 4)

    def test_stop_while_out_of_turns_asks_nothing_more(self):
        cancel = threading.Event()
        with self.assertRaises(AgentFailure):
            self.run_script(
                [("search_knowledge", {"query": "refund"}), cancel.set], *self.searches(4),
                cancel=cancel, wrap_up=True, then=["never"],
            )
        self.assertEqual(AIUsage.objects.filter(application=self.app).count(), 4)

    def test_the_chat_answer_keeps_a_turn_back(self):
        from platform_core.ai import stream_chat_answer

        script = Script(*self.searches(5), then=["From what was read."])
        session = SimpleNamespace(
            emit=lambda *a: None, cancel=threading.Event(), message_id=self.message.pk
        )
        with self.scripted(script):
            answer, citations = stream_chat_answer(
                self.owner, self.app, self.config, "unused", "How long?", [], [], session
            )
        self.assertEqual(answer, "From what was read.")
        self.assertEqual([c["id"] for c in citations], [str(self.source.pk)])
        self.assertEqual([call["max_turns"] for call in script.calls], [harness.TURNS, 1])

    def test_a_provider_failure_names_only_its_class(self):
        class Leaky:
            async def run(self, *args, **kwargs):
                raise RuntimeError("the prompt was: How long do refunds take?")

        with patch.object(harness, "runtime_for", return_value=Leaky()):
            with self.assertRaises(AgentFailure) as failed:
                run_agent(
                    user=self.owner, app=self.app, config=self.config, token="unused",
                    purpose="chat", scope=ToolScope(user_id=self.owner.pk, app_id=self.app.pk),
                    question="q", citations=[], history=[], emit=lambda *a: None,
                    max_tokens=1024, prompt_characters=1,
                )
        self.assertEqual(str(failed.exception), "RuntimeError")
        self.assertNotIn("refunds", repr(failed.exception))

    def test_steps_go_with_their_message_and_need_one(self):
        self.run_script([("search_knowledge", {"query": "refund"})], "A.", message=False)
        self.assertFalse(AgentStep.objects.exists())
        self.run_script([("search_knowledge", {"query": "refund"})], "A.")
        self.assertEqual(AgentStep.objects.count(), 1)
        self.message.delete()
        self.assertFalse(AgentStep.objects.exists())

    def test_a_message_deleted_while_it_was_answered_keeps_no_steps(self):
        def gone():
            ChatMessage.objects.filter(pk=self.message.pk).delete()

        _, result = self.run_script([("search_knowledge", {"query": "refund"}), gone], "A.")
        self.assertEqual(result.answer, "A.")
        self.assertFalse(AgentStep.objects.exists())

    def test_a_tool_that_breaks_is_a_failed_step_and_the_run_carries_on(self):
        def broken(scope, args):
            raise RuntimeError("index unavailable")

        spec = ToolSpec(
            name="search_knowledge", description="d", run=broken,
            schema=SEARCH_KNOWLEDGE.schema, purposes=SEARCH_KNOWLEDGE.purposes,
        )
        with patch.object(registry, "TOOLS", {"search_knowledge": spec}):
            script, result = self.run_script(
                [("search_knowledge", {"query": "refund"})], "Answered without it."
            )
        self.assertEqual(result.answer, "Answered without it.")
        self.assertEqual([s.outcome for s in self.steps()], ["failed"])

    def test_steps_that_cannot_be_written_never_fail_the_answer(self):
        from django.db import DatabaseError

        with (
            patch.object(AgentStep.objects, "bulk_create", side_effect=DatabaseError),
            self.assertLogs("platform_core.agent_runtime.harness", "WARNING") as logged,
        ):
            _, result = self.run_script([("search_knowledge", {"query": "refund"})], "A.")
        self.assertEqual(result.answer, "A.")
        self.assertEqual(logged.records[0].event, "agent_steps_not_kept")
        self.assertEqual(logged.records[0].exception_type, "DatabaseError")

    def test_the_chat_answer_runs_on_the_harness(self):
        from platform_core.ai import stream_chat_answer

        script = Script([("search_knowledge", {"query": "refund"})], "Thirty days.")
        session = SimpleNamespace(
            emit=lambda *a: None, cancel=threading.Event(), message_id=self.message.pk
        )
        with self.scripted(script):
            answer, citations = stream_chat_answer(
                self.owner, self.app, self.config, "unused", "How long?", [], [], session
            )
        self.assertEqual(answer, "Thirty days.")
        self.assertEqual([c["id"] for c in citations], [str(self.source.pk)])
        self.assertEqual(script.given, ["fetch_source", "search_knowledge"])
        self.assertEqual([s.tool for s in self.steps()], ["search_knowledge"])
        self.assertEqual(AgentStep.objects.get().purpose, "chat")


class RuntimeWiringTests(SimpleTestCase):
    """The real runtimes are given exactly the tools and turns the harness chose."""

    scope = ToolScope(user_id="u", app_id="a")

    def test_claude_with_no_tools_gets_no_server_and_one_turn(self):
        from claude_agent_sdk import ResultMessage

        seen = {}

        async def query(prompt, options):
            seen["options"] = options
            yield ResultMessage(
                subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                num_turns=1, session_id="s", total_cost_usd=0,
                usage={"input_tokens": 3, "output_tokens": 2}, result="From evidence.",
            )

        import asyncio

        for tools, servers, allowed, turns in (
            ([], {}, [], 1),
            (None, {"brain"}, claude_runtime.ALLOWED_TOOLS, 3),
            ([SEARCH_KNOWLEDGE], {"brain"}, ["mcp__brain__search_knowledge"], 3),
        ):
            with self.subTest(tools=tools), patch.object(claude_runtime, "query", query):
                _, answer = asyncio.run(
                    claude_runtime.run(
                        "claude-sonnet-5", "q", [], "sk-ant-test", [], self.scope,
                        CitationRecorder(self.scope), lambda *a: None,
                        tools=tools, max_turns=3,
                    )
                )
                options = seen["options"]
                self.assertEqual(set(options.mcp_servers), set(servers))
                self.assertEqual(options.allowed_tools, allowed)
                self.assertEqual(options.max_turns, turns)
                self.assertEqual(options.tools, [])
                self.assertEqual(answer, "From evidence.")

    def test_claude_out_of_turns_hands_back_what_its_turns_cost(self):
        import asyncio

        from claude_agent_sdk import ProcessError, ResultMessage

        def ending(subtype):
            async def query(prompt, options):
                yield ResultMessage(
                    subtype=subtype, duration_ms=1, duration_api_ms=1, is_error=True,
                    num_turns=3, session_id="s", total_cost_usd=0,
                    usage={"input_tokens": 300, "output_tokens": 40}, result=None,
                )
                # As the CLI does: an error result, then a non-zero exit.
                raise ProcessError("exit", exit_code=1)

            return query

        def run():
            return asyncio.run(
                claude_runtime.run(
                    "claude-sonnet-5", "q", [], "sk-ant-test", [], self.scope,
                    CitationRecorder(self.scope), lambda *a: None, max_turns=3,
                )
            )

        with patch.object(claude_runtime, "query", ending("error_max_turns")):
            with self.assertRaises(TurnLimit) as limit:
                run()
        self.assertEqual(str(limit.exception), "TurnLimit")
        usage = limit.exception.usages
        self.assertEqual([(u.prompt_tokens, u.completion_tokens) for u in usage], [(300, 40)])
        # Any other error result still fails as it did.
        with patch.object(claude_runtime, "query", ending("error_during_execution")):
            with self.assertRaises(ProcessError):
                run()

    def test_openai_with_no_tools_is_sent_none_and_one_turn(self):
        from platform_core.agent_runtime.openai_runtime import build_tools

        self.assertEqual(build_tools(self.scope, None, lambda *a: None, []), [])
        self.assertEqual(
            [t.name for t in build_tools(self.scope, None, lambda *a: None, [FETCH_SOURCE])],
            ["fetch_source"],
        )
        captured = {}

        def streamed(agent, input, max_turns, run_config):
            captured.update(tools=agent.tools, max_turns=max_turns)
            raise RuntimeError("stop here")

        import asyncio

        with patch.object(openai_runtime.Runner, "run_streamed", side_effect=streamed):
            with self.assertRaises(RuntimeError):
                asyncio.run(
                    openai_runtime.run(
                        "gpt-5", "q", [], "key", [], self.scope, None, lambda *a: None,
                        tools=[], max_turns=4,
                    )
                )
        self.assertEqual(captured, {"tools": [], "max_turns": 1})


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    AI_GATEWAY=GATEWAY,
)
class ScorecardProbeTests(TestCase):
    """The scorecard's "Agent guardrail probes passing", on a small workspace, so a
    probe that stops matching the code fails here rather than only in the gate."""

    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        ApplicationGrant.objects.create(application=self.other, user=self.owner, role="owner")
        for title in ("Consent export", "Consent retention"):
            add_knowledge(self.owner, self.app.pk, title, f"{title}: consent is recorded.")
        self.ws = SimpleNamespace(
            app=self.app, other=self.other, people={"owner": self.owner, "viewer": self.viewer}
        )

    def test_every_probe_passes_and_leaves_nothing_behind(self):
        from platform_core.scorecard import agent_guardrails

        values, probes = agent_guardrails(self.ws)
        self.assertEqual(values, {"agent_probes_passed": 100.0}, probes)
        self.assertEqual(len(probes), 30)
        self.assertFalse(AgentStep.objects.exists())
        self.assertFalse(AIUsage.objects.exists())
        self.assertFalse(AIBudget.objects.exists())
        self.assertTrue(
            ApplicationGrant.objects.filter(application=self.app, user=self.viewer).exists()
        )

    def test_a_broken_guard_fails_its_probe(self):
        from platform_core.scorecard import agent_guardrails

        # Stop no longer reaches the recorder: the run keeps reading after it.
        with patch.object(CitationRecorder, "__init__", _ignoring_cancel):
            _, probes = agent_guardrails(self.ws)
        self.assertFalse(probes["Stop runs no further tool"])
        self.assertTrue(probes["the tool budget holds"])


_RECORDER_INIT = CitationRecorder.__init__


def _ignoring_cancel(self, scope, budget=MAX_TOOL_CALLS, cancel=None):
    _RECORDER_INIT(self, scope, budget=budget, cancel=None)


class HarnessMetricTests(Measured, TestCase):
    """Every admitted run reports how it ended (`harness.measured`), with registry
    names and counts only; a run never admitted reports nothing."""

    # HarnessTests' own setting up and script runner, without its tests again.
    scripted = HarnessTests.scripted
    run_script = HarnessTests.run_script
    searches = HarnessTests.searches

    def setUp(self):
        HarnessTests.setUp(self)
        self.start_measuring()

    def runs(self):
        return {
            (p.attributes["gen_ai.operation.name"], p.attributes["digitalbrain.mode"],
             p.attributes["digitalbrain.outcome"]): p.count
            for p in self.points("digitalbrain.agent.run.duration")
        }

    def counted(self, name, *keys):
        return {
            tuple(p.attributes[key] for key in keys): p.value
            for p in self.points(name)
        }

    def test_an_answer_reports_its_run_turns_and_each_tool_call(self):
        self.run_script(
            [("search_knowledge", {"query": "refund"})],
            [("fetch_source", {"source_id": str(self.source.pk)})],
            "Thirty days.",
        )
        self.assertEqual(self.runs(), {("chat", "tools", "answered"): 1})
        self.assertEqual(
            self.counted("digitalbrain.agent.turns", "gen_ai.operation.name", "digitalbrain.mode"),
            {("chat", "tools"): 3},
        )
        self.assertEqual(
            self.counted("digitalbrain.agent.tool.calls", "gen_ai.tool.name",
                         "digitalbrain.outcome"),
            {("search_knowledge", "found"): 1, ("fetch_source", "found"): 1},
        )
        self.assertOnlyRegistered()

    def test_each_way_a_run_ends_is_named(self):
        self.run_script(*self.searches(5), wrap_up=True, then=["From what was read."])
        with self.assertRaises(AgentFailure):
            self.run_script(*self.searches(3), turns_asked=2)
        cancel = threading.Event()
        self.run_script([("search_knowledge", {"query": "refund"}), cancel.set], "Partial.",
                        cancel=cancel)

        def revoke():
            ApplicationGrant.objects.filter(application=self.app, user=self.owner).delete()

        with self.assertRaises(Http404):
            self.run_script([("search_knowledge", {"query": "refund"}), revoke], "Never kept.")
        self.assertEqual(self.runs(), {
            ("chat", "tools", "wrapped_up"): 1,
            ("chat", "tools", "turn_limit"): 1,
            ("chat", "tools", "stopped"): 1,
            ("chat", "tools", "refused"): 1,
        })
        self.assertEqual(set(harness.OUTCOMES) >= {o for *_, o in self.runs()}, True)

    def test_a_provider_failure_is_failed_and_one_turn_is_one_call(self):
        class Broken:
            async def run(self, *args, **kwargs):
                raise RuntimeError("down")

        with patch.object(harness, "runtime_for", return_value=Broken()):
            with self.assertRaises(AgentFailure):
                run_agent(
                    user=self.owner, app=self.app, config=self.config, token="unused",
                    purpose="chat", scope=ToolScope(user_id=self.owner.pk, app_id=self.app.pk),
                    question="q", citations=[], history=[], emit=lambda *a: None,
                    max_tokens=1024, prompt_characters=1,
                )
        self.run_script("From the evidence.", turns_asked=1)
        self.assertEqual(self.runs(), {
            ("chat", "tools", "failed"): 1, ("chat", "one_call", "answered"): 1,
        })

    def test_a_run_never_admitted_reports_nothing(self):
        with self.assertRaises(Http404):
            self.run_script("never", user=self.admin)
        self.assertEqual(self.runs(), {})

    def test_metrics_that_break_never_fail_the_run(self):
        from platform_core import metrics

        recorded = metrics.record

        def breaks(name, *args, **kwargs):
            if name.startswith("digitalbrain.agent."):
                raise RuntimeError("exporter gone")
            return recorded(name, *args, **kwargs)

        with (
            patch.object(metrics, "record", side_effect=breaks),
            self.assertLogs("platform_core.agent_runtime.harness", "WARNING") as logged,
        ):
            _, result = self.run_script("Fine.")
        self.assertEqual(result.answer, "Fine.")
        self.assertEqual(logged.records[0].event, "agent_metrics_failed")


class CitationNumberTests(TestCase):
    """A chat answer cites by number, and the page numbers the run's list: a tool's
    reply numbers its passages by that list, and the answer ends numbered as the
    page will show it (`CitationRecorder.numbered`)."""

    scripted = HarnessTests.scripted

    def setUp(self):
        HarnessTests.setUp(self)
        self.policy = self.source
        self.rota = add_knowledge(
            self.owner, self.app.pk, "Holiday rota", "The refund desk rota for the holidays."
        )
        self.other = add_knowledge(
            self.owner, self.app.pk, "Escalations", "Refund escalations go to the duty manager."
        )

    def seed(self, *entries):
        return [
            {"id": str(e.pk), "title": e.title, "digest": e.digest, "excerpt": e.content}
            for e in entries
        ]

    def run_script(self, *turns, seed=(), then=(), wrap_up=False, instructions=None,
                   purpose="chat"):
        script = Script(*turns, then=then)
        with self.scripted(script):
            result = run_agent(
                user=self.owner, app=self.app, config=self.config, token="unused",
                purpose=purpose,
                scope=ToolScope(user_id=self.owner.pk, app_id=self.app.pk, purpose="chat"),
                question="How long do refunds take?", citations=self.seed(*seed), history=[],
                emit=lambda *a: None, max_tokens=1024, prompt_characters=200,
                wrap_up=wrap_up, instructions=instructions,
            )
        return script, result

    def titles(self, result):
        return [c["title"] for c in result.citations]

    def test_a_searched_passage_is_numbered_after_the_seeded_evidence(self):
        script, result = self.run_script(
            [("search_knowledge", {"query": "refund requests expire"})],
            "Thirty days [2].",
            seed=[self.rota],
        )
        self.assertIn(f"[2] id={self.policy.pk} title=Refund policy", script.tool_text[0])
        self.assertEqual(result.answer, "Thirty days [2].")
        self.assertEqual(self.titles(result)[1], "Refund policy")

    def test_a_passage_already_held_keeps_its_number(self):
        script, _ = self.run_script(
            [("search_knowledge", {"query": "refund requests expire"})], "A.",
            seed=[self.policy],
        )
        self.assertIn(f"[1] id={self.policy.pk} title=Refund policy", script.tool_text[0])

    def test_a_fetched_window_is_numbered_too(self):
        script, result = self.run_script(
            [("fetch_source", {"source_id": str(self.policy.pk)})], "Thirty days [2].",
            seed=[self.rota],
        )
        self.assertTrue(script.tool_text[0].startswith("[2] Refund policy (characters"))
        self.assertEqual(self.titles(result), ["Holiday rota", "Refund policy"])

    def test_a_source_that_stops_verifying_moves_every_later_marker_onto_its_source(self):
        def change_rota():
            KnowledgeEntry.objects.filter(pk=self.rota.pk).update(digest="changed")

        _, result = self.run_script(
            [("search_knowledge", {"query": "refund requests expire"}), change_rota],
            "Thirty days [3], escalate [1], rota [2].",
            seed=[self.other, self.rota],
        )
        # The rota was [2] and no longer verifies: the policy, [3], is shown as [2],
        # and the marker to the rota is taken out rather than left on the policy.
        self.assertEqual(self.titles(result), ["Escalations", "Refund policy"])
        self.assertEqual(result.answer, "Thirty days [2], escalate [1], rota .")

    def test_a_marker_group_keeps_what_it_can(self):
        _, result = self.run_script("Both [1, 9] and [9].", seed=[self.policy])
        self.assertEqual(result.answer, "Both [1] and .")

    def test_cited_sources_are_kept_past_the_cap_and_uncited_ones_fill_it(self):
        from platform_core.agent_runtime import tools as tool_module

        with patch.object(tool_module, "MAX_CITATIONS", 2):
            _, result = self.run_script(
                "Rota [2] and escalations [3].", seed=[self.policy, self.rota, self.other]
            )
        self.assertEqual(self.titles(result), ["Holiday rota", "Escalations"])
        self.assertEqual(result.answer, "Rota [1] and escalations [2].")

    def test_out_of_turns_the_turn_kept_back_shares_the_turns_numbers(self):
        searches = [[("search_knowledge", {"query": "refund requests expire"})]] * 5
        script, result = self.run_script(
            *searches, seed=[self.other], wrap_up=True, then=["Escalate [1]; thirty days [2]."]
        )
        _, last = script.calls
        # The turn kept back is shown the list the page will show, in the same order.
        self.assertEqual(last["evidence"], [c["id"] for c in result.citations])
        self.assertEqual(self.titles(result)[:2], ["Escalations", "Refund policy"])
        self.assertEqual(result.answer, "Escalate [1]; thirty days [2].")

    def test_an_agent_that_cites_by_id_is_left_as_it_wrote(self):
        _, result = self.run_script(
            '{"note": "see [7]"}', seed=[self.policy], instructions="Answer in JSON."
        )
        self.assertEqual(result.answer, '{"note": "see [7]"}')
        self.assertEqual(self.titles(result), ["Refund policy"])
