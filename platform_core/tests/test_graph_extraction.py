"""Extraction runs per source, and the ask has to fit the output budget.

The bug these pin: one call over the whole corpus asked the model to be
exhaustive across every source at once. It answered with 29,769 completion
tokens against a budget of 8,192, which drove the CLI into an auto-continue
loop and returned nothing usable - after 829 seconds and real provider charges.
"""

import json
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings

from platform_core.graph_ai import (
    EXTRACTION_TOKENS,
    MAX_QUOTE_CHARACTERS,
    MAX_RELATIONSHIPS,
    RELATIONSHIPS_PER_SOURCE,
    enrich_graph,
)
from platform_core.graphs import build_graph, sources_for
from platform_core.workbench import add_knowledge

from . import test_documents


class ExtractionBudgetTests(SimpleTestCase):
    def test_the_per_source_ask_fits_the_output_budget(self):
        """If the ask exceeds the cap the model overruns it and the call is wasted."""
        # ~4 characters per token, plus generous room for the other fields and syntax.
        worst_case = RELATIONSHIPS_PER_SOURCE * (MAX_QUOTE_CHARACTERS + 400) / 4
        self.assertLess(worst_case, EXTRACTION_TOKENS * 0.75)

    def test_the_prompt_states_the_same_limits_the_validator_enforces(self):
        """These drifted apart once already, and every run failed because of it."""
        from platform_core.graph_ai import EXTRACTION_INSTRUCTIONS

        self.assertIn(str(RELATIONSHIPS_PER_SOURCE), EXTRACTION_INSTRUCTIONS)
        self.assertIn(str(MAX_QUOTE_CHARACTERS), EXTRACTION_INSTRUCTIONS)


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class PerSourceExtractionTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.first = add_knowledge(
            self.owner, self.app.pk, "Alpha", "Service Alpha sends messages to Queue Beta."
        )
        self.second = add_knowledge(
            self.owner, self.app.pk, "Gamma", "Service Gamma depends on Service Alpha."
        )
        self.config = type(
            "Config",
            (),
            {
                "configured_by_id": self.owner.pk,
                "configured_by": self.owner,
                "provider": "claude",
                "model": "claude-sonnet-5",
            },
        )()

    def enrich(self, responses):
        entries = list(sources_for(self.app.pk))
        data, quality = build_graph(entries, len(entries))
        with patch("platform_core.ai.invoke_ai", side_effect=responses) as invoke:
            result = enrich_graph(self.app.pk, self.config, entries, data, quality)
        return result, invoke

    def answer(self, entry, quote, subject, obj):
        return json.dumps(
            {
                "relationships": [
                    {
                        "subject": subject,
                        "relation": "depends on",
                        "object": obj,
                        "source_id": str(entry.pk),
                        "quote": quote,
                    }
                ]
            }
        )

    def test_each_source_is_extracted_in_its_own_call(self):
        (data, quality), invoke = self.enrich(
            [
                self.answer(self.first, self.first.content, "Service Alpha", "Queue Beta"),
                self.answer(self.second, self.second.content, "Service Gamma", "Service Alpha"),
            ]
        )
        self.assertEqual(invoke.call_count, 2)
        # Each call carries exactly one source, not the whole corpus.
        for call in invoke.call_args_list:
            self.assertEqual(len(call.args[4]), 1)
        self.assertEqual(quality["semantic_relationships"], 2)

    def test_a_source_the_model_fails_on_does_not_lose_the_whole_run(self):
        """Degrading to fewer relationships beats degrading to none."""
        with self.assertRaises(ValidationError):
            self.enrich(["not json at all", "also not json"])

    def test_more_relationships_than_the_per_source_cap_are_refused(self):
        overflowing = json.dumps(
            {
                "relationships": [
                    {
                        "subject": "A",
                        "relation": "uses",
                        "object": "B",
                        "source_id": str(self.first.pk),
                        "quote": "x",
                    }
                ]
                * (RELATIONSHIPS_PER_SOURCE + 1)
            }
        )
        with self.assertRaises(ValidationError):
            self.enrich([overflowing])

    def test_a_quote_longer_than_the_cap_is_rejected_by_verification(self):
        long_quote = "y" * (MAX_QUOTE_CHARACTERS + 1)
        (data, quality), _ = self.enrich(
            [
                self.answer(self.first, long_quote, "Service Alpha", "Queue Beta"),
                self.answer(self.second, self.second.content, "Service Gamma", "Service Alpha"),
            ]
        )
        self.assertEqual(quality["rejected_relationships"], 1)
        self.assertEqual(quality["semantic_relationships"], 1)

    def test_extraction_stops_once_the_overall_cap_is_reached(self):
        entries = list(sources_for(self.app.pk))
        data, quality = build_graph(entries, len(entries))
        full = json.dumps(
            {
                "relationships": [
                    {
                        "subject": "Service Alpha",
                        "relation": "sends messages to",
                        "object": "Queue Beta",
                        "source_id": str(self.first.pk),
                        "quote": self.first.content,
                    }
                ]
            }
        )
        with patch("platform_core.graph_ai.MAX_RELATIONSHIPS", 1):
            with patch("platform_core.ai.invoke_ai", return_value=full) as invoke:
                enrich_graph(self.app.pk, self.config, entries, data, quality)
        # The second source is never asked for once the cap is filled.
        self.assertEqual(invoke.call_count, 1)

    def test_the_overall_cap_still_bounds_the_merged_result(self):
        self.assertGreaterEqual(MAX_RELATIONSHIPS, RELATIONSHIPS_PER_SOURCE)
