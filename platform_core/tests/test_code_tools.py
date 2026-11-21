"""Code Factory's code tools and the agents that use them (agentic plan, Phase 1).

The tools are read against a real snapshot in the database; the agents are driven
by the scripted model (`agent_runtime.scripted`), so what a run reads, records and
may change is exercised with no provider.
"""

import json
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.test import TestCase, override_settings

from platform_core import code_factory_agents
from platform_core.agent_runtime import code_tools, harness, registry
from platform_core.agent_runtime.code_tools import NO_FILE, NO_SNAPSHOT
from platform_core.agent_runtime.scripted import Script
from platform_core.agent_runtime.tools import CitationRecorder, ToolScope, run_tool
from platform_core.code_factory_agents import (
    TOOL_GUIDANCE,
    consult,
    numbered_files,
    run_implementation,
    uses_tools,
)
from platform_core.models import (
    AgentStep,
    AIConfiguration,
    AIUsage,
    ApplicationFeature,
    ApplicationGrant,
    ChangePlan,
    CodeFile,
    CodeRelationship,
    CodeRepository,
    CodeSnapshot,
    FactoryRun,
    PlanItem,
    RunEvent,
    RunPhase,
)

from . import test_documents

SETTINGS = dict(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    AI_GATEWAY={
        "max_calls_per_application": 4,
        "max_calls_per_provider": 16,
        "slot_wait_seconds": 0,
        "admission_minutes": 15,
    },
)

CONSENT = """from .audit import record


def consent_given(patient):
    return bool(patient.consents)


class ConsentError(Exception):
    pass
"""

EXPORT = """from .consent import consent_given


def export_everything(patient):
    return {"patient": patient.id}
"""

TEST_EXPORT = """from app.export import export_everything


def test_export_everything():
    assert export_everything
"""


def snapshot_for(app, owner, name, files):
    repository = CodeRepository.objects.create(
        application=app, provider="local", external_id=name, name=name,
        source_url="https://example.invalid/", added_by=owner, status="ready",
    )
    snapshot = CodeSnapshot.objects.create(number=1, repository=repository, commit_sha="c" * 40)
    rows = {}
    for path, (content, symbols) in files.items():
        rows[path] = CodeFile.objects.create(
            snapshot=snapshot, path=path, language="python", digest=path,
            content=content, lines=content.count("\n") + 1, symbols=symbols,
        )
    return snapshot, rows


class Fixture:
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.snapshot, self.files = snapshot_for(
            self.app, self.owner, "acme/carepath",
            {
                "src/app/consent.py": (
                    CONSENT,
                    [
                        {"name": "consent_given", "line": 4, "kind": "function"},
                        {"name": "ConsentError", "line": 8, "kind": "class"},
                    ],
                ),
                "src/app/export.py": (
                    EXPORT, [{"name": "export_everything", "line": 4, "kind": "function"}]
                ),
                "tests/test_export.py": (
                    TEST_EXPORT, [{"name": "test_export_everything", "line": 4, "kind": "function"}]
                ),
            },
        )
        CodeRelationship.objects.create(
            snapshot=self.snapshot, source=self.files["src/app/export.py"],
            target=self.files["src/app/consent.py"], kind="import", confidence="static",
            detail="consent_given",
        )
        ApplicationGrant.objects.create(application=self.other, user=self.owner, role="owner")
        self.foreign, _ = snapshot_for(
            self.other, self.owner, "elsewhere/secret",
            {"src/app/consent.py": ("SECRET = 'another application'\n", [])},
        )

    def scope(self, snapshot=None, user=None, purpose="plan_drafting"):
        return ToolScope(
            user_id=(user or self.owner).pk, app_id=self.app.pk, purpose=purpose,
            snapshot_id=(snapshot or self.snapshot).pk if snapshot is not False else None,
        )

    def call(self, spec, args, scope=None):
        recorder = CitationRecorder(scope or self.scope())
        return run_tool(spec, recorder.scope, args, recorder), recorder


@override_settings(**SETTINGS)
class CodeToolTests(Fixture, TestCase):
    def test_list_files_from_the_root_and_under_a_prefix(self):
        text, recorder = self.call(code_tools.LIST_FILES, {"prefix": ""})
        self.assertIn("3 file(s)", text)
        self.assertIn("src/app/consent.py (10 lines)", text)
        text, _ = self.call(code_tools.LIST_FILES, {"prefix": "./tests/"})
        self.assertEqual(text.splitlines()[1:], ["tests/test_export.py (6 lines)"])
        text, recorder = self.call(code_tools.LIST_FILES, {"prefix": "nowhere/"})
        self.assertIn("No files", text)
        self.assertEqual(recorder.steps[0]["outcome"], "nothing")

    def test_a_long_listing_says_how_many_more(self):
        with patch.object(code_tools, "MAX_LISTED", 2):
            text, _ = self.call(code_tools.LIST_FILES, {"prefix": ""})
        self.assertIn("[1 more; list a narrower prefix]", text)

    def test_read_file_numbers_lines_and_says_where_to_go_on(self):
        text, recorder = self.call(
            code_tools.READ_FILE, {"path": "src/app/consent.py", "start_line": 4, "end_line": 5}
        )
        self.assertEqual(
            text.splitlines(),
            [
                "src/app/consent.py (lines 4-5 of 9, as indexed):",
                "    4 | def consent_given(patient):",
                "    5 |     return bool(patient.consents)",
                "[4 more lines; read again from start_line=6]",
            ],
        )
        self.assertEqual(recorder.steps[0]["outcome"], "found")
        whole, _ = self.call(
            code_tools.READ_FILE, {"path": "/src/app/export.py", "start_line": 0, "end_line": 0}
        )
        self.assertIn("lines 1-5 of 5", whole)
        self.assertNotIn("more lines", whole)

    def test_a_read_is_held_to_a_window(self):
        with patch.object(code_tools, "MAX_LINES", 3):
            text, _ = self.call(
                code_tools.READ_FILE,
                {"path": "src/app/consent.py", "start_line": 1, "end_line": 99},
            )
        self.assertIn("lines 1-3 of 9", text)

    def test_a_path_is_only_ever_a_key_in_this_runs_snapshot(self):
        for path in ("../../etc/passwd", "src/app/missing.py", "", "C:\\Windows\\win.ini"):
            with self.subTest(path=path):
                text, _ = self.call(
                    code_tools.READ_FILE, {"path": path, "start_line": 1, "end_line": 0}
                )
                self.assertEqual(text, NO_FILE)

    def test_another_applications_snapshot_reads_as_no_files(self):
        """The same path exists there; scope is the snapshot of this application."""
        scope = self.scope(self.foreign)
        text, _ = self.call(
            code_tools.READ_FILE, {"path": "src/app/consent.py", "start_line": 1, "end_line": 0},
            scope,
        )
        self.assertEqual(text, NO_FILE)
        listed, _ = self.call(code_tools.LIST_FILES, {"prefix": ""}, scope)
        self.assertNotIn("consent", listed)
        found, _ = self.call(code_tools.SEARCH_CODE, {"query": "SECRET", "limit": 5}, scope)
        self.assertNotIn("another application", found)

    def test_a_run_with_no_snapshot_reads_nothing(self):
        text, _ = self.call(code_tools.LIST_FILES, {"prefix": ""}, self.scope(False))
        self.assertEqual(text, NO_SNAPSHOT)

    def test_search_ranks_the_file_with_more_of_the_words_first(self):
        # Both files name consent_given; only the one defining it imports record.
        text, _ = self.call(code_tools.SEARCH_CODE, {"query": "consent_given record", "limit": 5})
        lines = text.splitlines()
        self.assertIn("2 file(s) mention consent_given record", lines[0])
        self.assertEqual(lines[1], "src/app/consent.py")
        self.assertIn("def consent_given(patient):", text)
        self.assertLess(text.index("src/app/consent.py"), text.index("src/app/export.py"))
        nothing, recorder = self.call(code_tools.SEARCH_CODE, {"query": "zeppelin", "limit": 5})
        self.assertIn("Nothing in this run's snapshot mentions zeppelin", nothing)
        self.assertEqual(recorder.steps[0]["outcome"], "nothing")
        empty, _ = self.call(code_tools.SEARCH_CODE, {"query": " ", "limit": 5})
        self.assertEqual(empty, "Provide words to search for.")

    def test_find_symbol_exactly_then_ignoring_case(self):
        text, _ = self.call(code_tools.FIND_SYMBOL, {"name": "ConsentError"})
        self.assertIn("src/app/consent.py:8 class ConsentError", text)
        folded, _ = self.call(code_tools.FIND_SYMBOL, {"name": "consenterror"})
        self.assertIn("src/app/consent.py:8 class ConsentError", folded)
        missing, _ = self.call(code_tools.FIND_SYMBOL, {"name": "nowhere"})
        self.assertIn("No definition", missing)

    def test_callers_and_covering_tests(self):
        text, _ = self.call(code_tools.CALLERS, {"path": "src/app/consent.py"})
        self.assertIn("src/app/export.py (import: consent_given)", text)
        none, _ = self.call(code_tools.CALLERS, {"path": "src/app/export.py"})
        self.assertIn("Nothing in this run's snapshot depends on", none)
        tests, _ = self.call(code_tools.TESTS_FOR, {"path": "src/app/export.py"})
        self.assertIn("tests/test_export.py", tests)
        missing, _ = self.call(code_tools.TESTS_FOR, {"path": "src/app/gone.py"})
        self.assertEqual(missing, NO_FILE)

    def test_a_revoked_grant_or_a_feature_off_refuses_the_next_read(self):
        recorder = CitationRecorder(self.scope())
        run_tool(code_tools.LIST_FILES, recorder.scope, {"prefix": ""}, recorder)
        ApplicationFeature.objects.create(application=self.app, key="code_factory", enabled=False)
        with self.assertRaises(PermissionDenied):
            run_tool(code_tools.LIST_FILES, recorder.scope, {"prefix": ""}, recorder)
        self.assertIsInstance(recorder.revoked, PermissionDenied)
        ApplicationFeature.objects.all().delete()
        ApplicationGrant.objects.filter(application=self.app, user=self.owner).delete()
        with self.assertRaises(Http404):
            run_tool(code_tools.READ_FILE, recorder.scope, {"path": "src/app/export.py"}, recorder)

    def test_someone_who_cannot_change_code_factory_cannot_read_through_it(self):
        with self.assertRaises(PermissionDenied):
            self.call(code_tools.LIST_FILES, {"prefix": ""}, self.scope(user=self.viewer))

    def test_the_tools_serve_code_factory_alone(self):
        for spec in code_tools.CODE_TOOLS:
            with self.subTest(tool=spec.name):
                self.assertEqual(spec.purposes, frozenset({"plan_drafting"}))
                self.assertIn(spec, registry.for_scope(self.scope()))
                self.assertNotIn(spec, registry.for_scope(self.scope(purpose="chat")))


@override_settings(**SETTINGS)
class ConsultTests(Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.config = AIConfiguration.objects.create(
            application=self.app, purpose="plan_drafting", provider="claude",
            model="claude-sonnet-5", input_rate=Decimal("3"), output_rate=Decimal("15"),
            configured_by=self.owner,
        )
        plan = ChangePlan.objects.create(
            application=self.app, author=self.owner, title="Check consent", proposal="p",
            validation="v", digest="d", status="approved",
        )
        PlanItem.objects.create(
            plan=plan, sequence=0, category="stated", title="Check consent before export",
            explanation="e", change_summary="Refuse the export without consent.",
            targets=["src/app/export.py"],
        )
        self.run = FactoryRun.objects.create(
            application=self.app, requested_by=self.owner, plan=plan,
            ticket_external_id="CARE-401", proposed_repository="acme/carepath",
            repository_confirmed=True, status="preparing", code_snapshot=self.snapshot,
        )
        credential = patch("platform_core.ai.provider_credential", return_value="sk-ant-test")
        credential.start()
        self.addCleanup(credential.stop)
        self.targets = [{"path": "src/app/export.py", "text": EXPORT, "sha": None}]

    def implement(self, script):
        with (
            override_settings(CODE_FACTORY_AGENT_TOOLS=True),
            patch.object(harness, "runtime_for", return_value=script),
        ):
            return run_implementation(self.run, None, self.targets, order={}, references=[])

    def test_off_by_default_every_agent_is_the_one_shot_call(self):
        self.assertFalse(uses_tools(self.run, "implementation"))
        with patch.object(code_factory_agents, "ask", return_value="{}") as ask:
            consult(self.run, "implementation", "Do it.", "q", [], {})
        ask.assert_called_once()

    def test_only_the_three_agents_with_a_snapshot_take_tools(self):
        with override_settings(CODE_FACTORY_AGENT_TOOLS=True):
            for phase in ("implementation", "tests", "review"):
                self.assertTrue(uses_tools(self.run, phase))
            for phase in ("work_order", "verification", "delivery", "triage"):
                self.assertFalse(uses_tools(self.run, phase))
            self.run.code_snapshot = None
            self.assertFalse(uses_tools(self.run, "implementation"))

    def test_the_implementation_reads_the_snapshot_and_changes_only_its_targets(self):
        new = EXPORT.replace("    return", "    if not consent_given(patient):\n"
                             "        raise PermissionError\n    return")
        script = Script(
            [
                ("callers", {"path": "src/app/consent.py"}),
                ("read_file", {"path": "src/app/consent.py", "start_line": 1, "end_line": 0}),
            ],
            [("tests_for", {"path": "src/app/export.py"})],
            json.dumps(
                {"files": [{"file": "1", "content": new}, {"file": "9", "content": "x"}]}
            ),
        )
        changes = self.implement(script)
        # The answer named a tenth file; only the numbered target is a change.
        self.assertEqual([change["path"] for change in changes], ["src/app/export.py"])
        self.assertIn("consent_given(patient)", changes[0]["content"])
        self.assertEqual(
            script.given,
            ["callers", "find_symbol", "list_files", "read_file", "search_code", "tests_for"],
        )
        self.assertTrue(script.instructions.endswith(TOOL_GUIDANCE))
        steps = list(AgentStep.objects.filter(run=self.run).order_by("sequence"))
        self.assertEqual(
            [(s.phase, s.tool, s.outcome) for s in steps],
            [
                ("implementation", "callers", "found"),
                ("implementation", "read_file", "found"),
                ("implementation", "tests_for", "found"),
            ],
        )
        self.assertEqual(steps[1].arguments["path"], {"characters": len("src/app/consent.py")})
        notes = list(RunEvent.objects.filter(run=self.run).values_list("message", flat=True))
        self.assertIn("Implementation read: read_file src/app/consent.py", notes)
        self.assertIn("Implementation: 3 read(s) of the snapshot over 3 model turn(s).", notes)
        phase = RunPhase.objects.get(run=self.run, name="implementation")
        self.assertEqual((phase.status, phase.prompt_tokens, phase.completion_tokens),
                         ("ok", 300, 60))
        self.assertEqual(AIUsage.objects.filter(purpose="plan_drafting").count(), 3)
        # The snapshot is what it was: reading changes nothing.
        self.assertEqual(CodeFile.objects.get(pk=self.files["src/app/export.py"].pk).content,
                         EXPORT)

    def test_a_failed_model_call_fails_the_phase_without_its_detail(self):
        class Broken:
            def run(self, *args, **kwargs):
                raise RuntimeError("the prompt said CARE-401 export_everything")

        with self.assertRaises(ValidationError) as raised:
            self.implement(Broken())
        message = " ".join(raised.exception.messages)
        self.assertIn("Implementation: the model call failed (RuntimeError)", message)
        self.assertNotIn("export_everything", message)
        self.assertEqual(RunPhase.objects.get(run=self.run, name="implementation").status,
                         "failed")

    def test_a_grant_revoked_mid_run_ends_the_phase(self):
        def revoke():
            ApplicationGrant.objects.filter(application=self.app, user=self.owner).delete()

        script = Script([revoke, ("list_files", {"prefix": ""})], '{"files": []}')
        with self.assertRaises(Http404):
            self.implement(script)
        self.assertEqual(
            list(AgentStep.objects.filter(run=self.run).values_list("outcome", flat=True)),
            ["refused"],
        )

    def test_the_review_reads_with_tools_under_its_own_phase(self):
        script = Script([("find_symbol", {"name": "consent_given"})], '{"verdicts": []}')
        receipt = {}
        with (
            override_settings(CODE_FACTORY_AGENT_TOOLS=True),
            patch.object(harness, "runtime_for", return_value=script),
        ):
            answer = consult(self.run, "review", "Review it.", "q", [], receipt)
        self.assertEqual(answer, '{"verdicts": []}')
        self.assertEqual((receipt["turns"], receipt["tool_calls"]), (2, 1))
        self.assertEqual(
            list(AgentStep.objects.filter(run=self.run).values_list("phase", "tool")),
            [("review", "find_symbol")],
        )
        self.assertIn(
            "Review read: find_symbol consent_given",
            RunEvent.objects.filter(run=self.run).values_list("message", flat=True),
        )

    def test_the_numbered_files_are_what_the_agent_may_change(self):
        """The same contract as the one-shot call: by number, never by path."""
        numbered = numbered_files(self.targets)
        self.assertEqual([item["id"] for item in numbered], ["1"])
        self.assertNotIn("src/app/consent.py", [item["title"] for item in numbered])


@override_settings(**SETTINGS)
class SettingTests(TestCase):
    def test_the_setting_must_be_a_boolean(self):
        import tempfile
        from pathlib import Path

        from django.core.exceptions import ImproperlyConfigured

        from digitalbrain.configuration import load_config

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.toml"
            for value, refused in (("true", False), ("false", False), ('"yes"', True)):
                with self.subTest(value=value):
                    path.write_text(
                        f'mode = "development"\nhosts = ["localhost"]\n'
                        f"code_factory_agent_tools = {value}\n"
                    )
                    with patch.dict("os.environ", {"DIGITAL_BRAIN_CONFIG": str(path)}):
                        if refused:
                            with self.assertRaises(ImproperlyConfigured):
                                load_config()
                        else:
                            self.assertIsInstance(
                                load_config()["code_factory_agent_tools"], bool
                            )


class FinalTurnTests(TestCase):
    """The Claude runtime joins every turn's text for chat, and keeps only the
    last turn's for an agent asked for JSON."""

    def stream(self):
        from claude_agent_sdk import ResultMessage, StreamEvent

        def delta(text):
            return StreamEvent(
                uuid="u", session_id="s",
                event={
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": text},
                },
            )

        start = StreamEvent(uuid="u", session_id="s", event={"type": "message_start"})

        async def query(prompt, options):
            self.options = options
            for message in (start, delta("Let me read the consent service. "), start,
                            delta('{"files": []}')):
                yield message
            yield ResultMessage(
                subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                num_turns=2, session_id="s", usage={"input_tokens": 5, "output_tokens": 3},
                result='{"files": []}',
            )

        return query

    def answer(self, **options):
        import asyncio

        from platform_core.agent_runtime import claude_runtime

        scope = ToolScope(user_id="u", app_id="a", purpose="plan_drafting")
        with patch.object(claude_runtime, "query", self.stream()):
            _, answer = asyncio.run(
                claude_runtime.run(
                    "claude-sonnet-5", "q", [], "sk-ant-test", [], scope,
                    CitationRecorder(scope), lambda *a: None, tools=[], **options,
                )
            )
        return answer

    def test_chat_keeps_every_turn_and_an_agent_its_last(self):
        self.assertEqual(self.answer(), 'Let me read the consent service. {"files": []}')
        self.assertEqual(
            self.answer(instructions="Return JSON.", timeout=900, final_only=True),
            '{"files": []}',
        )
        self.assertEqual(self.options.system_prompt, "Return JSON.")
        self.assertEqual(self.options.env["API_TIMEOUT_MS"], "900000")
