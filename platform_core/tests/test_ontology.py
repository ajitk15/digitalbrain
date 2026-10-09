"""The ontology types extraction, merges names by type, and lets retrieval walk two hops."""

import json
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings

from platform_core import graph_ai, ontology
from platform_core.graph_ai import EXTRACTION_INSTRUCTIONS, citations_from, enrich_graph
from platform_core.graphs import build_graph, sources_for
from platform_core.ontology import (
    RELATIONS,
    TYPES,
    aliases_for,
    merge_entities,
    normalise,
    typed,
)
from platform_core.workbench import add_knowledge

from . import test_documents


def entity(node_id, label, kind="Component", source="s1"):
    return {"id": node_id, "kind": "entity", "type": kind, "label": label, "knowledge_id": source}


def related(start, end, relation="depends on", source="s1", quote="q"):
    return {
        "source": start,
        "target": end,
        "relation": relation,
        "knowledge_id": source,
        "digest": "d",
        "evidence": quote,
        "inferred": True,
    }


class RegistryTests(SimpleTestCase):
    def test_phrasings_become_one_relation(self):
        for phrase in ("uses", "Relies on", "depends on", "sends messages to"):
            self.assertEqual(
                typed("Component", phrase, "component"), ("Component", "depends on", "Component")
            )

    def test_type_aliases_are_accepted(self):
        self.assertEqual(typed("bug", "violates", "NFR"), ("Ticket", "violates", "Requirement"))

    def test_anything_outside_the_registry_is_refused(self):
        self.assertIsNone(typed("Component", "is owned by", "Component"))
        self.assertIsNone(typed("Person", "depends on", "Component"))
        self.assertIsNone(typed(None, "depends on", "Component"))

    def test_the_wrong_direction_is_refused_not_reversed(self):
        self.assertIsNone(typed("Requirement", "violates", "Ticket"))

    def test_references_is_the_catch_all(self):
        self.assertEqual(typed("Runbook", "mentions", "Ticket")[1], "references")

    def test_the_prompt_states_every_type_and_relation(self):
        """The prompt and the validator read one registry, so they cannot drift."""
        for name in TYPES:
            self.assertIn(name, EXTRACTION_INSTRUCTIONS)
        for name in RELATIONS:
            self.assertIn(f"'{name}'", EXTRACTION_INSTRUCTIONS)

    def test_normalise_ignores_case_and_separators(self):
        self.assertEqual(normalise("CarePath-API_green."), "carepath api green")
        self.assertEqual(normalise("The Referrals API"), "referrals api")


class MergeTests(SimpleTestCase):
    def test_the_same_name_and_type_from_two_sources_is_one_node(self):
        data = {
            "nodes": [
                entity("a", "Referrals API", source="s1"),
                entity("b", "referrals-api", source="s2"),
                entity("c", "Audit log", source="s1"),
            ],
            "edges": [
                related("c", "a", source="s1", quote="first"),
                related("b", "c", relation="references", source="s2", quote="second"),
            ],
        }
        self.assertEqual(merge_entities("app", data), 1)
        merged = next(n for n in data["nodes"] if n.get("aliases"))
        self.assertEqual(merged["aliases"], ["Referrals API", "referrals-api"])
        self.assertEqual(merged["knowledge_ids"], ["s1", "s2"])
        # Joined across two documents: no single document to open.
        self.assertNotIn("knowledge_id", merged)
        # Each edge keeps its own source and quote, so it is verified as before.
        self.assertEqual(
            {(e["knowledge_id"], e["evidence"]) for e in data["edges"]},
            {("s1", "first"), ("s2", "second")},
        )
        self.assertTrue(all(merged["id"] in (e["source"], e["target"]) for e in data["edges"]))

    def test_a_different_type_is_never_merged(self):
        data = {
            "nodes": [entity("a", "Audit trail"), entity("b", "Audit trail", "Requirement", "s2")],
            "edges": [],
        }
        self.assertEqual(merge_entities("app", data), 0)

    def test_tickets_one_digit_apart_are_two_tickets(self):
        data = {
            "nodes": [entity("a", "CARE-401", "Ticket"), entity("b", "CARE-412", "Ticket", "s2")],
            "edges": [],
        }
        self.assertEqual(merge_entities("app", data), 0)

    def test_a_longer_name_is_a_different_thing(self):
        """carepath-api is not carepath-api-green: CARE-444 is exactly that mistake."""
        data = {
            "nodes": [entity("a", "carepath-api"), entity("b", "carepath-api-green", source="s2")],
            "edges": [],
        }
        self.assertEqual(merge_entities("app", data), 0)

    def test_two_colours_of_one_deployment_are_two_deployments(self):
        data = {
            "nodes": [
                entity("a", "carepath-api-blue"),
                entity("b", "carepath-api-green", source="s2"),
            ],
            "edges": [],
        }
        self.assertEqual(merge_entities("app", data), 0)

    def test_near_identical_names_merge(self):
        data = {
            "nodes": [
                entity("a", "Notification service"),
                entity("b", "Notifications service"),
                entity("c", "Authentication gateway"),
                entity("d", "Authentcation gateway", source="s2"),
            ],
            "edges": [],
        }
        self.assertEqual(merge_entities("app", data), 2)

    def test_an_edge_between_two_names_for_one_thing_is_dropped(self):
        data = {
            "nodes": [entity("a", "Referrals API"), entity("b", "Referrals-API", source="s2")],
            "edges": [related("a", "b")],
        }
        merge_entities("app", data)
        self.assertEqual(data["edges"], [])

    def test_untyped_entities_from_older_versions_are_left_alone(self):
        data = {
            "nodes": [
                {"id": "a", "kind": "entity", "label": "Queue"},
                {"id": "b", "kind": "entity", "label": "Queue"},
            ],
            "edges": [],
        }
        self.assertEqual(merge_entities("app", data), 0)

    def test_merged_names_become_component_aliases(self):
        data = {
            "nodes": [
                {
                    "id": "m",
                    "kind": "entity",
                    "type": "Component",
                    "label": "carepath-api-green",
                    "aliases": ["CarePath API-Green", "carepath-api-green", "carepath api greens"],
                }
            ]
        }
        self.assertEqual(
            aliases_for(data, {"carepath-api-green": "component:green"}),
            {"carepath api-green": "component:green", "carepath api greens": "component:green"},
        )
        self.assertEqual(aliases_for(data, {"carepath-api": "component:api"}), {})


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class TypedGraphTests(TestCase):
    """Three sources that only connect through a shared component and a shared file."""

    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.ticket = add_knowledge(
            self.owner,
            self.app.pk,
            "CARE-425",
            "Referral status changes skip the audit trail.\nCARE-425 affects Referrals API.",
        )
        self.design = add_knowledge(
            self.owner, self.app.pk, "Design", "Referrals API is implemented in referrals.py."
        )
        self.requirements = add_knowledge(
            self.owner, self.app.pk, "Requirements", "NFR-03 is implemented in referrals.py."
        )
        self.answers = {
            str(self.ticket.pk): [
                ("CARE-425", "Ticket", "affects", "Referrals API", "Component"),
                # Outside the ontology: a person, and a relation it does not have.
                ("CARE-425", "Person", "affects", "Referrals API", "Component"),
                ("CARE-425", "Ticket", "is owned by", "Referrals API", "Component"),
            ],
            str(self.design.pk): [
                ("Referrals API", "Component", "is implemented in", "referrals.py", "Code"),
            ],
            str(self.requirements.pk): [
                ("NFR-03", "Requirement", "implemented in", "referrals.py", "file"),
            ],
        }
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

    def respond(self, *args, **kwargs):
        citation = args[4][0]
        content = next(e.content for e in sources_for(self.app.pk) if str(e.pk) == citation["id"])
        quote = content.splitlines()[-1]
        return json.dumps(
            {
                "relationships": [
                    {
                        "subject": subject,
                        "subject_type": subject_type,
                        "relation": relation,
                        "object": obj,
                        "object_type": object_type,
                        "source_id": citation["id"],
                        "quote": quote.rstrip("."),
                    }
                    for subject, subject_type, relation, obj, object_type in self.answers[
                        citation["id"]
                    ]
                ]
            }
        )

    def enrich(self):
        entries = list(sources_for(self.app.pk))
        data, quality = build_graph(entries, len(entries))
        with patch("platform_core.ai.invoke_ai", side_effect=self.respond):
            return enrich_graph(self.app.pk, self.config, entries, data, quality)

    def test_extraction_is_typed_canonical_and_merged(self):
        data, quality = self.enrich()
        self.assertEqual(quality["semantic_relationships"], 3)
        self.assertEqual(quality["outside_ontology"], 2)
        self.assertEqual(quality["ontology"], ontology.ONTOLOGY_VERSION)
        self.assertEqual(data["ontology"], ontology.ONTOLOGY_VERSION)
        relations = {e["relation"] for e in data["edges"] if e.get("inferred")}
        self.assertEqual(relations, {"affects", "implemented in"})
        # "Referrals API" from two sources and "referrals.py" from two are one node each.
        self.assertEqual(quality["merged_entities"], 2)
        entities = [n for n in data["nodes"] if n["kind"] == "entity"]
        self.assertEqual(
            sorted((n["type"], n["label"]) for n in entities),
            [
                ("Code", "referrals.py"),
                ("Component", "Referrals API"),
                ("Requirement", "NFR-03"),
                ("Ticket", "CARE-425"),
            ],
        )

    def test_a_question_walks_two_hops_to_the_requirement(self):
        """CARE-425 -> Referrals API -> referrals.py <- NFR-03, with no shared word."""
        data, _ = self.enrich()
        cited = citations_from(self.app.pk, data, 1, "What does CARE-425 affect?")
        by_source = {citation["id"]: citation for citation in cited}
        self.assertIn(str(self.requirements.pk), by_source)
        walked = by_source[str(self.requirements.pk)]
        self.assertEqual(walked["hops"], 2)
        self.assertIn("Reached by: CARE-425 affects Referrals API", walked["excerpt"])
        self.assertEqual(walked["quote"], "NFR-03 is implemented in referrals.py")

    def test_one_hop_alone_does_not_reach_it(self):
        data, _ = self.enrich()
        with patch.object(graph_ai, "MAX_HOPS", 1):
            cited = citations_from(self.app.pk, data, 1, "What does CARE-425 affect?")
        self.assertNotIn(str(self.requirements.pk), {c["id"] for c in cited})

    def test_a_hub_is_not_walked_through(self):
        data, _ = self.enrich()
        with patch.object(graph_ai, "MAX_FANOUT", 1):
            cited = citations_from(self.app.pk, data, 1, "What does CARE-425 affect?")
        self.assertNotIn(str(self.requirements.pk), {c["id"] for c in cited})

    def test_a_walked_citation_is_verified_like_any_other(self):
        data, _ = self.enrich()
        self.requirements.active = False
        self.requirements.save()
        cited = citations_from(self.app.pk, data, 1, "What does CARE-425 affect?")
        self.assertNotIn(str(self.requirements.pk), {c["id"] for c in cited})

    def test_another_applications_graph_data_cites_nothing_here(self):
        data, _ = self.enrich()
        self.assertEqual(citations_from(self.other.pk, data, 1, "What does CARE-425 affect?"), [])

    def test_a_published_graph_is_prepared_once_and_dropped_when_saved(self):
        from django.utils import timezone

        from platform_core.graph_ai import graph_citations
        from platform_core.models import GraphRevision

        data, quality = self.enrich()
        revision = GraphRevision.objects.create(
            application=self.app,
            number=1,
            fingerprint="x",
            data=data,
            quality=quality,
            published_at=timezone.now(),
        )
        with patch.object(graph_ai, "prepare", wraps=graph_ai.prepare) as prepare:
            first = graph_citations(self.app.pk, "What does CARE-425 affect?")
            self.assertEqual(graph_citations(self.app.pk, "What does CARE-425 affect?"), first)
            self.assertEqual(prepare.call_count, 1)
            revision.save()
            graph_citations(self.app.pk, "What does CARE-425 affect?")
            self.assertEqual(prepare.call_count, 2)
        self.assertIn(str(self.requirements.pk), {citation["id"] for citation in first})
