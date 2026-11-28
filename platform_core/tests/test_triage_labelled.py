"""The labelled triage pack (Phase D of `agentic-ai-next-plan.md`).

A fresh application has no incident whose cause a person confirmed, so
`triage_pack` could never measure triage there. `benchmarks/triage-pack.json`
holds CarePath incidents and changes written as a ServiceNow import stores them,
each open incident labelled with the record that is its cause. These tests pin
that loading stores them exactly as an import would and only once, and test every
label against the records as the operations graph itself reads them: a labelled
change comes before its incident on the same component, a planted distractor
does or does not as the pack says, and a scored run is a reference of its own.
"""

import json
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from platform_core import measure_agents, ops_graph, triage_pack
from platform_core.models import AIConfiguration, KnowledgeEntry
from platform_core.serviceops import incident_queryset

from . import test_documents


class LabelledPackTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.pack = triage_pack.load_pack()
        AIConfiguration.objects.create(
            application=self.app, purpose="serviceops_triage", provider="claude",
            model="claude-sonnet-5", input_rate=Decimal("3"), output_rate=Decimal("15"),
            configured_by=self.owner,
        )
        for patcher in (
            patch("platform_core.graph_ai.graph_citations", return_value=[]),
            patch("platform_core.ai.provider_credential", return_value="sk-ant-test"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def numbered(self):
        """{record number: its stored entry} for every pack record here."""
        found = {}
        for record in self.pack["records"]:
            entry = KnowledgeEntry.objects.filter(
                application=self.app, source=triage_pack.record_source(self.pack, record),
                active=True,
            ).first()
            if entry is not None:
                found[record["fields"]["Number"]] = entry
        return found

    def test_loading_stores_each_record_as_an_import_would_and_only_once(self):
        self.assertEqual(triage_pack.load_incidents(self.owner, self.app), (14, 0))
        self.assertEqual(triage_pack.load_incidents(self.owner, self.app), (0, 14))
        held = self.numbered()
        self.assertEqual(len(held), 14)
        incident = held["INC0011201"]
        # Title, a blank line, the header the connector writes, a blank line, the text.
        self.assertTrue(incident.content.startswith(
            "Patient records failing to open, 5xx rising\n\nType: Incident\n"
            "Number: INC0011201\nState: New\n"
        ))
        self.assertIn("incident.do%3Fsys_id%3D", incident.source)
        self.assertIn("change_request.do%3Fsys_id%3D", held["CHG0030112"].source)
        self.assertEqual(incident_queryset(self.app).count(), 6)
        self.assertEqual(ops_graph.change_queryset(self.app).count(), 8)

    def test_a_record_the_pack_changed_replaces_the_one_held(self):
        triage_pack.load_incidents(self.owner, self.app)
        changed = json.loads(json.dumps(self.pack))
        changed["records"][1]["text"] += " Wards are now phoning results."
        self.assertEqual(triage_pack.load_incidents(self.owner, self.app, changed), (1, 13))
        source = triage_pack.record_source(changed, changed["records"][1])
        rows = KnowledgeEntry.objects.filter(application=self.app, source=source)
        self.assertEqual((rows.count(), rows.filter(active=True).count()), (2, 1))

    def test_every_label_names_a_record_the_operations_graph_links_as_the_pack_says(self):
        triage_pack.load_incidents(self.owner, self.app)
        held = self.numbered()
        nodes, edges = ops_graph.plan(self.app)
        before = {
            (source, target) for source, target, relation in edges if relation == "before"
        }

        def precedes(change, incident):
            return (f"change:{held[change].pk}", f"incident:{held[incident].pk}") in before

        for number, label in self.pack["cases"].items():
            cause = label["cause"]
            with self.subTest(case=number, cause=cause):
                if cause.startswith("CHG"):
                    self.assertTrue(precedes(cause, number))
                else:
                    # A resolved incident, linked as similar to the one it explains.
                    self.assertIn("Resolved", held[cause].content)
                    self.assertIn(
                        (f"incident:{held[number].pk}", f"incident:{held[cause].pk}",
                         "similar"),
                        edges,
                    )
        # The distractors: on another component, outside the two days, or a second
        # change before the same incident that the model has to tell apart.
        self.assertFalse(precedes("CHG0030113", "INC0011201"))
        self.assertFalse(precedes("CHG0030122", "INC0011202"))
        self.assertTrue(precedes("CHG0030132", "INC0011203"))
        self.assertTrue(precedes("CHG0030142", "INC0011204"))

    def test_labelled_cases_are_the_loaded_pairs_and_nothing_else(self):
        self.assertEqual(triage_pack.labelled_cases(self.app), [])
        triage_pack.load_incidents(self.owner, self.app)
        held = self.numbered()
        pairs = triage_pack.labelled_cases(self.app)
        self.assertEqual(
            [(incident.pk, cause.pk) for incident, cause in pairs],
            [(held[number].pk, held[label["cause"]].pk)
             for number, label in self.pack["cases"].items()],
        )
        # A restricted cause is not answerable, so its case is not scored.
        KnowledgeEntry.objects.filter(pk=held["CHG0030121"].pk).update(sensitivity="restricted")
        self.assertEqual(len(triage_pack.labelled_cases(self.app)), 4)

    def test_a_labelled_run_is_scored_against_the_labels_as_a_reference_of_its_own(self):
        triage_pack.load_incidents(self.owner, self.app)

        def one_shot(user, app_id, purpose, question, citations, **options):
            from platform_core.services import record_ai_usage

            options["receipt"].update(prompt_tokens=1000, completion_tokens=200,
                                      provider="claude", model="claude-sonnet-5",
                                      request_id=f"r-{len(question)}-{len(citations)}")
            record_ai_usage(
                actor=user, application_id=app_id, provider="Anthropic",
                model="claude-sonnet-5", request_id=options["receipt"]["request_id"],
                purpose=purpose, input_tokens=1000, output_tokens=200,
                amount=Decimal("0.006"),
            )
            return json.dumps({"hypotheses": []})

        with patch("platform_core.ai.invoke_ai", side_effect=one_shot):
            record = triage_pack.run(self.owner, self.app, ("one_shot",), labelled=True)
        self.assertEqual(record["labels"], "pack")
        self.assertEqual(record["pack"], triage_pack.pack_digest(self.pack))
        self.assertEqual(record["metrics"]["one_shot"]["cases"], 5)
        self.assertEqual({row["mode"] for row in record["details"]}, {"one_shot"})

    def test_without_the_pack_loaded_a_labelled_run_says_how_to_load_it(self):
        from django.core.exceptions import ValidationError

        with self.assertRaises(ValidationError) as refused:
            triage_pack.run(self.owner, self.app, ("one_shot",), labelled=True)
        self.assertIn("--load-incidents", " ".join(refused.exception.messages))

    def test_the_command_loads_and_measure_agents_then_runs_it_labelled(self):
        out = StringIO()
        call_command("triage_pack", str(self.app.pk), "--as", "owner", "--load-incidents",
                     stdout=out)
        self.assertIn("14 record(s) added", out.getvalue())
        self.assertTrue(measure_agents.triage_labelled(self.app))
        said = dict((check.pack, said) for check, ok, said in measure_agents.readiness(self.app))
        self.assertEqual(said["triage_pack"], "5 labelled incidents (run with --labelled)")

    def test_a_viewer_may_not_load_records(self):
        from django.core.exceptions import PermissionDenied

        with self.assertRaises(PermissionDenied):
            triage_pack.load_incidents(self.viewer, self.app)
        self.assertFalse(incident_queryset(self.app).exists())
