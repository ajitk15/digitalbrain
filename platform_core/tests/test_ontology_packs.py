"""An application may follow an industry domain, and AI extraction follows it.

A pack is the core registry plus a domain's own types and relations - never
instead of it - so the core prompt, cache rubric and fingerprint must not move
for an application that chose none. A pack's own relations refuse a negated
quote unless the relation itself is a negative one, and a patient is accepted
only under a masking placeholder.
"""

import json
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from platform_core import graph_ai, masking, ontology
from platform_core.graph_ai import (
    EXTRACTION_INSTRUCTIONS,
    enrich_graph,
    instructions,
    judge,
    rubric,
)
from platform_core.graphs import build_graph, fingerprint, sources_for
from platform_core.models import (
    Application,
    ApplicationGrant,
    AuditEvent,
    DocumentAIResult,
    Job,
    OrganizationMember,
    User,
)
from platform_core.ontology import CORE, HEALTHCARE, MASKED, PACKS, US_INSURANCE
from platform_core.workbench import add_knowledge

from . import test_documents


def item(subject, subject_type, relation, obj, object_type, quote, source_id="s1"):
    return {
        "subject": subject,
        "subject_type": subject_type,
        "relation": relation,
        "object": obj,
        "object_type": object_type,
        "source_id": source_id,
        "quote": quote,
    }


def source(text, title="Source"):
    return {"s1": {"id": "s1", "title": title, "excerpt": text, "digest": "d"}}


class RegistryTests(SimpleTestCase):
    def test_every_pack_is_sound(self):
        for pack in PACKS.values():
            with self.subTest(pack=pack.key or "core"):
                self.assertEqual(pack.check(), [])

    def test_the_core_registry_is_unchanged_by_packs_existing(self):
        self.assertEqual(CORE.types, ontology.TYPES)
        self.assertEqual(CORE.relations, ontology.RELATIONS)
        self.assertEqual(ontology.prompt(), CORE.prompt())
        self.assertEqual(instructions(), EXTRACTION_INSTRUCTIONS)
        self.assertEqual(instructions(CORE), EXTRACTION_INSTRUCTIONS)
        self.assertIn(
            "the ticket, runbook, incident or change the evidence is", EXTRACTION_INSTRUCTIONS
        )
        # The rubric answers are cached under: unchanged, so nothing is paid twice.
        self.assertEqual(rubric(), "ontology-1")
        self.assertEqual(rubric(CORE), "ontology-1")

    def test_a_pack_adds_to_the_core_registry_and_never_replaces_it(self):
        for pack in (US_INSURANCE, HEALTHCARE):
            with self.subTest(pack=pack.key):
                self.assertLessEqual(set(ontology.TYPES), set(pack.types))
                self.assertLessEqual(set(ontology.RELATIONS), set(pack.relations))
                self.assertEqual(
                    pack.typed("bug", "violates", "NFR"), ("Ticket", "violates", "Requirement")
                )
                self.assertEqual(pack.relations["references"].subjects, pack.any)
                prompt = instructions(pack)
                self.assertIn("Component (", prompt)
                self.assertIn(pack.preference, prompt)
                self.assertNotEqual(rubric(pack), rubric(CORE))
        self.assertIn("Carrier (", instructions(US_INSURANCE))
        self.assertIn("Patient (", instructions(HEALTHCARE))
        self.assertNotIn("Carrier (", instructions(HEALTHCARE))

    def test_a_domain_word_wins_over_a_core_alias_for_something_else(self):
        self.assertEqual(CORE.entity_type("policy"), "Control")
        self.assertEqual(US_INSURANCE.entity_type("policy"), "Policy")
        self.assertEqual(US_INSURANCE.entity_type("regulation"), "Control")
        self.assertEqual(CORE.entity_type("procedure"), "Runbook")
        self.assertEqual(HEALTHCARE.entity_type("procedure"), "Procedure")
        self.assertEqual(HEALTHCARE.entity_type("SOP"), "Runbook")
        self.assertEqual(US_INSURANCE.entity_type("LossEvent"), "Loss event")

    def test_a_shared_phrasing_is_the_relation_whose_types_fit(self):
        self.assertEqual(US_INSURANCE.typed("Coverage", "covers", "Peril")[1], "covers peril")
        self.assertEqual(US_INSURANCE.typed("Runbook", "covers", "Component")[1], "covers")
        self.assertEqual(US_INSURANCE.typed("Claim", "concerns", "Risk")[1], "concerns")
        self.assertEqual(US_INSURANCE.typed("Ticket", "concerns", "Component")[1], "affects")
        # The wrong way round is still refused, not reversed.
        self.assertIsNone(US_INSURANCE.typed("Carrier", "issued by", "Policy"))
        self.assertIsNone(CORE.typed("Coverage", "covers", "Peril"))

    def test_the_masked_pattern_is_what_masking_writes_for_a_person(self):
        for kind in ("medical record number", "health plan member number", "NHS number",
                     "patient name"):
            with self.subTest(kind=kind):
                self.assertRegex(masking.placeholder(b"key", kind, "RGH-0142857"), MASKED)
        self.assertNotRegex(masking.placeholder(b"key", "email address", "a@b.example"), MASKED)
        self.assertEqual(masking.TOKEN_LETTERS, 8)


class JudgeTests(SimpleTestCase):
    def test_a_negated_quote_supports_none_of_a_packs_positive_relations(self):
        text = "Coverage A covers windstorm. Coverage A does not cover flood."
        kinds, reason = judge(
            item("Coverage A", "Coverage", "covers", "windstorm", "Peril",
                 "Coverage A covers windstorm"),
            source(text),
            US_INSURANCE,
        )
        self.assertEqual((kinds, reason), (("Coverage", "covers peril", "Peril"), None))
        _, reason = judge(
            item("Coverage A", "Coverage", "covers", "flood", "Peril",
                 "Coverage A does not cover flood"),
            source(text),
            US_INSURANCE,
        )
        self.assertEqual(reason, "negated quote")

    def test_a_negative_relation_is_what_a_negated_quote_supports(self):
        text = "The water exclusion does not cover flood damage."
        kinds, reason = judge(
            item("water exclusion", "Exclusion", "does not cover", "flood", "Peril", text),
            source(text),
            US_INSURANCE,
        )
        self.assertEqual((kinds, reason), (("Exclusion", "excludes", "Peril"), None))
        text = "Metformin is contraindicated in severe renal impairment."
        kinds, _ = judge(
            item("Metformin", "drug", "is contraindicated in", "severe renal impairment",
                 "Condition", text),
            source(text),
            HEALTHCARE,
        )
        self.assertEqual(kinds, ("Medication", "contraindicated with", "Condition"))

    def test_a_number_label_is_not_a_denial(self):
        text = "Policy No. HO-4512 is issued by Prairie Mutual."
        kinds, reason = judge(
            item("Policy No. HO-4512", "Policy", "is issued by", "Prairie Mutual", "insurer", text),
            source(text),
            US_INSURANCE,
        )
        self.assertEqual((kinds, reason), (("Policy", "issued by", "Carrier"), None))

    def test_core_relations_in_a_pack_refuse_exactly_what_they_always_did(self):
        text = "CARE-401 does not meet BR-05."
        for domain in (CORE, US_INSURANCE, HEALTHCARE):
            with self.subTest(domain=domain.key or "core"):
                kinds, reason = judge(
                    item("CARE-401", "Ticket", "does not meet", "BR-05", "Requirement", text),
                    source(text),
                    domain,
                )
                self.assertEqual((kinds, reason), (("Ticket", "violates", "Requirement"), None))

    def test_a_patient_is_accepted_only_under_a_masking_placeholder(self):
        text = (
            "[MRN-QZKTWBAF] was diagnosed with type 2 diabetes; "
            "Jane Roe was diagnosed with asthma."
        )
        kinds, reason = judge(
            item("[MRN-QZKTWBAF]", "Patient", "was diagnosed with", "type 2 diabetes", "Condition",
                 "[MRN-QZKTWBAF] was diagnosed with type 2 diabetes"),
            source(text),
            HEALTHCARE,
        )
        self.assertEqual((kinds, reason), (("Patient", "diagnosed with", "Condition"), None))
        _, reason = judge(
            item("Jane Roe", "Patient", "was diagnosed with", "asthma", "Condition",
                 "Jane Roe was diagnosed with asthma"),
            source(text),
            HEALTHCARE,
        )
        self.assertEqual(reason, "unmasked person")

    def test_a_policy_or_claim_may_be_the_subject_of_what_it_says(self):
        text = "This policy includes Coverage A - Dwelling."
        proposal = item("POL-2026-004512", "Policy", "includes", "Coverage A", "Coverage", text)
        kinds, reason = judge(proposal, source(text, title="POL-2026-004512"), US_INSURANCE)
        self.assertEqual((kinds, reason), (("Policy", "includes", "Coverage"), None))
        # Not under the core registry, which has no Policy to be.
        _, reason = judge(proposal, source(text, title="POL-2026-004512"), CORE)
        self.assertEqual(reason, "outside the ontology")
        self.assertIn("policy, claim, filing, form or guideline", instructions(US_INSURANCE))


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class ApplicationPackTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.contributor = User.objects.create_user("contributor")
        OrganizationMember.objects.create(
            organization_id=self.app.organization_id, user=self.contributor
        )
        ApplicationGrant.objects.create(
            application=self.app, user=self.contributor, role="contributor"
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

    def rubrics(self):
        rows = DocumentAIResult.objects.filter(application=self.app)
        return set(rows.values_list("rubric", flat=True))

    def choose(self, key):
        Application.objects.filter(pk=self.app.pk).update(ontology=key)

    def test_an_application_follows_its_pack_and_an_unknown_one_is_the_core(self):
        self.assertIs(ontology.for_application(self.app.pk), CORE)
        self.choose("us_insurance")
        self.assertIs(ontology.for_application(self.app.pk), US_INSURANCE)
        self.choose("mining")
        self.assertIs(ontology.for_application(self.app.pk), CORE)

    def test_only_a_pack_changes_the_fingerprint(self):
        add_knowledge(self.owner, self.app.pk, "Policy", "Coverage A covers windstorm.")
        before = fingerprint(self.app.pk)
        self.choose("us_insurance")
        insured = fingerprint(self.app.pk)
        self.choose("healthcare")
        self.assertNotIn(fingerprint(self.app.pk), {before, insured})
        self.choose("")
        self.assertEqual(fingerprint(self.app.pk), before)

    def respond(self, *args, **kwargs):
        self.asked.append(kwargs["instructions"])
        citation = args[4][0]
        return json.dumps(
            {
                "relationships": [
                    item("Coverage A", "Coverage", "covers", "windstorm", "Peril",
                         "Coverage A covers windstorm", citation["id"]),
                    item("Coverage A", "Coverage", "covers", "flood", "Peril",
                         "Coverage A does not cover flood", citation["id"]),
                ]
            }
        )

    def enrich(self, config):
        entries = list(sources_for(self.app.pk))
        data, quality = build_graph(entries, len(entries))
        with patch("platform_core.ai.invoke_ai", side_effect=self.respond):
            return enrich_graph(self.app.pk, config, entries, data, quality)

    def test_extraction_asks_judges_and_caches_under_the_pack(self):
        add_knowledge(
            self.owner,
            self.app.pk,
            "HO-3 declarations",
            "Coverage A covers windstorm.\nCoverage A does not cover flood.",
        )
        self.choose("us_insurance")
        self.asked = []
        data, quality = self.enrich(self.config)
        self.assertEqual(len(self.asked), 1)
        self.assertIn("Carrier (", self.asked[0])
        self.assertEqual(quality["semantic_relationships"], 1)
        self.assertEqual(quality["rejection_reasons"], {"negated quote": 1})
        self.assertEqual(data["ontology_pack"], "us_insurance")
        self.assertEqual(quality["ontology_pack"], "us_insurance")
        relations = {e["relation"] for e in data["edges"] if e.get("inferred")}
        self.assertEqual(relations, {"covers peril"})
        self.assertEqual(
            self.rubrics(),
            {"ontology-1-us_insurance-1"},
        )
        # Answers paid for under the pack are never carried into another one.
        self.choose("")
        self.asked = []
        data, quality = self.enrich(None)
        self.assertEqual(self.asked, [])
        self.assertNotIn("ontology_pack", data)
        self.assertFalse([e for e in data["edges"] if e.get("inferred")])

    def test_the_core_registry_still_extracts_as_before(self):
        add_knowledge(
            self.owner,
            self.app.pk,
            "HO-3",
            "Coverage A covers windstorm.\nCoverage A does not cover flood.",
        )
        self.asked = []
        data, quality = self.enrich(self.config)
        self.assertEqual(self.asked, [EXTRACTION_INSTRUCTIONS])
        self.assertNotIn("ontology_pack", data)
        self.assertNotIn("ontology_pack", quality)
        # The core registry has no Coverage or Peril.
        self.assertEqual(quality["rejection_reasons"], {"outside the ontology": 2})
        self.assertEqual(
            self.rubrics(),
            {graph_ai.rubric()},
        )

    def post_domain(self, key, user=None):
        if user is not None:
            self.client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")
        return self.client.post(
            reverse("graph-generate", args=[self.app.pk]), {"action": "ontology", "ontology": key}
        )

    def test_an_owner_changes_the_domain_and_it_is_audited_and_rebuilt(self):
        page = self.client.get(reverse("graph-generate", args=[self.app.pk])).content.decode()
        self.assertIn('id="industry-domain"', page)
        self.assertRegex(page, r'value=""[^>]*checked')
        response = self.post_domain("healthcare")
        self.assertRedirects(response, reverse("graph-generate", args=[self.app.pk]))
        self.app.refresh_from_db()
        self.assertEqual(self.app.ontology, "healthcare")
        event = AuditEvent.objects.get(action="graph.ontology_changed")
        self.assertEqual(event.details, {"from": "core", "to": "healthcare"})
        self.assertTrue(
            Job.objects.filter(kind="graph_refresh", application_id=self.app.pk).exists()
        )
        page = self.client.get(reverse("graph-generate", args=[self.app.pk])).content.decode()
        self.assertIn("Healthcare and life sciences</strong> ontology", page)

    def test_an_unknown_domain_is_refused(self):
        self.post_domain("mining")
        self.app.refresh_from_db()
        self.assertEqual(self.app.ontology, "")
        self.assertFalse(AuditEvent.objects.filter(action="graph.ontology_changed").exists())

    def test_only_an_owner_may_change_the_domain(self):
        self.client.force_login(
            self.contributor, backend="django.contrib.auth.backends.ModelBackend"
        )
        page = self.client.get(reverse("graph-generate", args=[self.app.pk])).content.decode()
        self.assertNotIn('id="industry-domain"', page)
        self.assertIn('id="graph-ontology"', page)
        self.assertEqual(self.post_domain("us_insurance").status_code, 403)
        self.app.refresh_from_db()
        self.assertEqual(self.app.ontology, "")

    def test_the_domain_is_not_changed_under_a_running_generation(self):
        from platform_core.models import KnowledgeGraph

        KnowledgeGraph.objects.create(application=self.app, status="queued")
        self.post_domain("us_insurance")
        self.app.refresh_from_db()
        self.assertEqual(self.app.ontology, "")


@override_settings(
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    CLAUDE_USE_HOST_LOGIN=False,
)
class CreationTests(TestCase):
    def setUp(self):
        from . import test_creation

        test_creation.ApplicationCreationTests.setUp(self)
        self.url = lambda: reverse("application-create", args=[self.org.pk])
        self.payload = lambda **kw: test_creation.ApplicationCreationTests.payload(self, **kw)

    def test_the_create_form_offers_every_domain_and_records_the_choice(self):
        page = self.client.get(self.url()).content.decode()
        for pack in PACKS.values():
            self.assertIn(pack.label, page)
        self.client.post(self.url(), self.payload(ontology="us_insurance"))
        app = Application.objects.get(name="Reporting")
        self.assertEqual(app.ontology, "us_insurance")
        event = AuditEvent.objects.get(action="application.created", resource_id=str(app.pk))
        self.assertEqual(event.details, {"ontology": "us_insurance"})

    def test_a_caller_that_names_no_domain_gets_the_core_registry(self):
        self.client.post(self.url(), self.payload())
        self.assertEqual(Application.objects.get(name="Reporting").ontology, "")

    def test_an_unknown_domain_creates_nothing(self):
        response = self.client.post(self.url(), self.payload(ontology="mining"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Application.objects.filter(name="Reporting").exists())
