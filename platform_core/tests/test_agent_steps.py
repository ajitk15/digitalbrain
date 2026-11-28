"""A run's tool calls on its own page (Phase F of `agentic-ai-next-plan.md`).

`agent_catalog.step_rows` turns a run's `AgentStep` rows into what the Code
Factory agent page, a code review and a triage run list: tool, the shape of its
arguments, outcome, size and time. Pinned here: a shape never becomes the text
that was asked - it holds none - and a repeat is counted as one.
"""

from types import SimpleNamespace

from django.test import SimpleTestCase

from platform_core.agent_catalog import shape_text, step_rows


def step(sequence, tool, arguments, outcome="found", characters=120, citations=0):
    return SimpleNamespace(sequence=sequence, tool=tool, arguments=arguments, outcome=outcome,
                           result_characters=characters, citations=citations, duration_ms=12)


class StepRowTests(SimpleTestCase):
    def test_a_shape_reads_as_lengths_numbers_and_ids_never_text(self):
        shaped = {"path": {"characters": 31}, "start_line": 1, "end_line": 0,
                  "source_id": {"id": "3f0c1c9e-2a55-4c21-9f1e-0c1d2e3f4a5b"}, "whole": True}
        self.assertEqual(
            shape_text(shaped),
            "end_line: 0, path: 31 characters, source_id: an id, start_line: 1, whole: True",
        )
        # Anything else a shape could hold is left out rather than shown.
        self.assertEqual(shape_text({"query": "the patient's name", "terms": ["x"]}), "")
        self.assertEqual(shape_text(None), "")

    def test_rows_say_each_outcome_by_its_label_and_count_repeats(self):
        rows, repeated = step_rows([
            step(1, "read_file", {"path": {"characters": 31}}),
            step(2, "read_file", {"path": {"characters": 31}}, outcome="repeat", characters=90),
            step(3, "search_code", {"query": {"characters": 12}}, outcome="nothing",
                 characters=0),
        ])
        self.assertEqual([row["outcome"] for row in rows],
                         ["Found", "Already shown", "Found nothing"])
        self.assertEqual(repeated, 1)
        self.assertEqual(rows[0]["arguments"], "path: 31 characters")
        self.assertEqual((rows[2]["characters"], rows[2]["duration_ms"]), (0, 12))


class StepOwnerTests(SimpleTestCase):
    """One `steps_for` pair names what a run's steps belong to (it was seven ids)."""

    def test_every_name_links_a_model_with_that_link_on_agent_steps(self):
        from platform_core.agent_runtime.harness import STEP_OWNERS, step_owner
        from platform_core.models import AgentStep

        fields = {field.name for field in AgentStep._meta.get_fields()}
        for name in STEP_OWNERS:
            with self.subTest(name=name):
                self.assertIn(name, fields)
                self.assertIs(AgentStep._meta.get_field(name).related_model, step_owner(name))

    def test_an_unknown_owner_is_refused_rather_than_its_steps_lost(self):
        from platform_core.agent_runtime.harness import keep_steps

        with self.assertRaises(ValueError):
            keep_steps(SimpleNamespace(pk=1), "chat", [{"sequence": 1}], ("incident", 7))
        # Nothing to keep, or nothing it belongs to, is simply nothing kept.
        self.assertEqual(keep_steps(SimpleNamespace(pk=1), "chat", [], ("run", 7)), 0)
        self.assertEqual(keep_steps(SimpleNamespace(pk=1), "chat", [{}], None), 0)
        self.assertEqual(keep_steps(SimpleNamespace(pk=1), "chat", [{}], ("run", None)), 0)
