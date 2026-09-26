"""ServiceOps evidence and triage stay verified and application-scoped."""

import json
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from platform_core.api_auth import issue
from platform_core.connector_kinds import servicenow_records
from platform_core.models import (
    AIConfiguration,
    ApiToken,
    ApplicationFeature,
    ApplicationGrant,
    KnowledgeEntry,
    TriageHypothesis,
    TriageRun,
    TriageVerdict,
)
from platform_core.serviceops import fingerprint, priority_tone
from platform_core.serviceops_triage import _parsed_hypotheses, _score, redact
from platform_core.workbench import add_knowledge

from . import test_documents


class FingerprintTests(SimpleTestCase):
    def test_unsafe_model_step_is_dropped_and_weak_score_abstains(self):
        pack = [
            {
                "id": "source-1",
                "kind": "related_open",
                "excerpt": "Queue is stalled today",
                "reasons": ["shared symptoms"],
                "as_of": "2026-09-25T10:00:00+00:00",
            }
        ]
        answer = json.dumps(
            {
                "hypotheses": [
                    {
                        "statement": "Queue may be stalled",
                        "next_step": "Restart the worker",
                        "citations": [{"id": "source-1", "quote": "Queue is stalled today"}],
                    }
                ]
            }
        )
        self.assertEqual(_parsed_hypotheses(answer, pack), [])
        supported = [{"citations": [{"id": "source-1", "quote": "Queue is stalled today"}]}]
        self.assertEqual(_score(pack, supported)[0], "insufficient")

    def test_model_input_redacts_common_private_values(self):
        clean = redact("a@b.com +1 415 555 0199 password=abc123 token:secret")
        self.assertNotIn("a@b.com", clean)
        self.assertNotIn("abc123", clean)
        self.assertNotIn("token:secret", clean)

    def test_volatile_ids_and_timestamps_do_not_change_signature(self):
        first = fingerprint("Queue timeout 1234", "2026-09-25 10:15 request 999 failed")
        second = fingerprint("Queue timeout 4567", "2026-09-26 11:25 request 888 failed")
        self.assertEqual(first, second)
        self.assertTrue(first)

    def test_priority_colors_accept_only_known_numeric_priorities(self):
        for number in range(1, 6):
            self.assertEqual(priority_tone(f"{number} - Label"), f"p{number}")
        self.assertEqual(priority_tone("unexpected"), "unknown")
        self.assertEqual(priority_tone("1 injected-class"), "unknown")
        self.assertEqual(priority_tone("1evil"), "unknown")


class ServiceNowIncidentMappingTests(SimpleTestCase):
    def test_bounded_history_backfill_deduplicates_pages(self):
        config = {
            "base_url": "https://acme.service-now.com",
            "auth": "basic",
            "identity": "svc",
            "table": "incident",
            "history_pages": 2,
        }
        first = [{"sys_id": f"id-{index}", "number": f"INC{index}"} for index in range(100)]
        second = [{"sys_id": "id-99", "number": "INC99"}, {"sys_id": "id-100", "number": "INC100"}]
        with patch(
            "platform_core.connector_kinds.api_json",
            side_effect=[
                {"result": first},
                {"result": second},
            ],
        ) as api:
            records = servicenow_records(config, "password")
        self.assertEqual(len(records), 101)
        self.assertIn("sysparm_offset=100", api.call_args.args[0])

    def test_incident_fields_enter_the_digested_body(self):
        config = {
            "base_url": "https://acme.service-now.com",
            "auth": "basic",
            "identity": "svc",
            "table": "incident",
            "title_field": "short_description",
            "body_field": "description",
        }
        row = {
            "sys_id": "abc123",
            "number": "INC0001",
            "short_description": "Queue timeout",
            "description": "Requests wait too long.",
            "business_service": "Orders",
            "cmdb_ci": "api-1",
            "state": "Resolved",
            "close_notes": "Restarted worker.\nState: New after checking backlog.",
        }
        with patch("platform_core.connector_kinds.api_json", return_value={"result": [row]}) as api:
            record = servicenow_records(config, "password")[0]
        self.assertIn("business_service", api.call_args.args[0])
        self.assertIn("Type: Incident", record.body)
        self.assertIn("Service: Orders", record.body)
        self.assertIn("CI: api-1", record.body)
        self.assertIn("Close notes: Restarted worker", record.body)
        self.assertNotIn("\nState: New", record.body)
        self.assertIn("Requests wait too long.", record.body)

    def test_reference_fields_arrive_as_display_values_and_are_read(self):
        """The shape a live instance returns under sysparm_display_value=true.

        The test above passes plain strings, which a real instance never sends
        for a reference field - so Service and CI were empty on every import.
        """
        config = {
            "base_url": "https://acme.service-now.com",
            "auth": "basic",
            "identity": "svc",
            "table": "incident",
            "title_field": "short_description",
            "body_field": "description",
        }

        def reference(name, table):
            return {
                "display_value": name,
                "link": f"https://acme.service-now.com/api/now/table/{table}/0123",
            }

        row = {
            "sys_id": "abc123",
            "number": "INC0010009",
            "short_description": "FHIR bulk export returning 504",
            "description": "Large exports time out.",
            "business_service": reference("CAREPATH_OPS", "cmdb_ci_service"),
            "cmdb_ci": reference("carepath-api-green", "cmdb_ci"),
            "assignment_group": reference("CAREOPS", "sys_user_group"),
            "state": "In Progress",
            "priority": "2 - High",
        }
        with patch("platform_core.connector_kinds.api_json", return_value={"result": [row]}):
            record = servicenow_records(config, "password")[0]
        self.assertIn("Service: CAREPATH_OPS", record.body)
        self.assertIn("CI: carepath-api-green", record.body)
        self.assertIn("Assignment group: CAREOPS", record.body)
        self.assertNotIn("api/now/table", record.body)

    def test_non_incident_tables_keep_their_existing_body_shape(self):
        config = {
            "base_url": "https://acme.service-now.com",
            "auth": "basic",
            "identity": "svc",
            "table": "problem",
            "title_field": "short_description",
            "body_field": "description",
        }
        row = {
            "sys_id": "abc123",
            "number": "PRB0001",
            "short_description": "Problem",
            "description": "Original text.",
        }
        with patch("platform_core.connector_kinds.api_json", return_value={"result": [row]}):
            record = servicenow_records(config, "password")[0]
        self.assertEqual(record.body, "Original text.")

    def test_change_request_carries_time_and_ci_in_digested_body(self):
        config = {
            "base_url": "https://acme.service-now.com",
            "auth": "basic",
            "identity": "svc",
            "table": "change_request",
        }
        row = {
            "sys_id": "change-1",
            "number": "CHG001",
            "short_description": "Deploy worker",
            "description": "Updated queue worker configuration",
            "cmdb_ci": "api-1",
            "start_date": "2026-09-25 08:00:00",
        }
        with patch("platform_core.connector_kinds.api_json", return_value={"result": [row]}):
            record = servicenow_records(config, "password")[0]
        self.assertIn("Type: Change", record.body)
        self.assertIn("CI: api-1", record.body)
        self.assertIn("Start: 2026-09-25 08:00:00", record.body)


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class ServiceOpsViewTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.url = reverse("serviceops", args=[self.app.pk])
        # ServiceOps AI on, as a new application starts; the model step skips,
        # and says so, when it is off.
        AIConfiguration.objects.create(
            application=self.app,
            purpose="serviceops_triage",
            provider="claude",
            model="claude-sonnet-5",
            input_rate=3,
            output_rate=15,
        )

    def triage(self, incident):
        """Press Triage, then let the serviceops worker lane do the run, as it would."""
        from platform_core.serviceops_triage import process_next_triage

        response = self.client.post(self.url, {"incident": str(incident.pk)})
        process_next_triage()
        return response

    def incident(
        self,
        title,
        *,
        state="New",
        close_notes="",
        app=None,
        service="Orders",
        number="",
        priority="",
    ):
        content = (
            f"{title}\n\nType: Incident\nNumber: {number}\nPriority: {priority}\n"
            f"State: {state}\nService: {service}\n"
            f"CI: api-1\nClose notes: {close_notes}\n\nQueue workers time out."
        )
        return add_knowledge(
            self.owner,
            (app or self.app).pk,
            title,
            content,
            source="https://acme.service-now.com/nav_to.do?uri=incident.do%3Fsys_id%3D123",
        )

    def test_imported_incident_number_appears_in_list_and_detail(self):
        incident = self.incident("Test incident", number="INC0010001", priority="4 - Low")
        response = self.client.get(self.url)
        self.assertContains(
            response,
            '<span class="incident-badge incident-number">INC0010001</span>',
            html=True,
        )
        self.assertContains(response, 'class="incident-badge incident-priority priority-p4"')
        self.assertContains(response, "4 - Low")
        self.assertContains(response, "Test incident")
        self.assertEqual(response.context["incidents"][0]["number"], "INC0010001")
        self.assertEqual(response.context["incidents"][0]["priority_tone"], "p4")
        detail = self.client.get(self.url, {"incident": str(incident.pk)})
        self.assertContains(
            detail,
            '<span class="incident-badge incident-number">INC0010001</span>',
            html=True,
        )

    def test_tab_shows_scoped_precedents_without_a_model_call(self):
        current = self.incident("Queue workers time out")
        prior = self.incident(
            "Queue workers timed out", state="Resolved", close_notes="Restarted the worker."
        )
        self.incident("Unresolved similar incident")
        with patch("platform_core.graph_ai.graph_citations", return_value=[]):
            response = self.client.get(self.url, {"incident": str(current.pk)})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Restarted the worker.")
        self.assertContains(response, reverse("knowledge-detail", args=[self.app.pk, prior.pk]))
        self.assertEqual([row["entry"].pk for row in response.context["precedents"]], [prior.pk])
        self.assertContains(response, "a match is a lead, not a cause")

    def test_foreign_and_revoked_access_are_not_disclosed(self):
        current = self.incident("Queue timeout")
        ApplicationGrant.objects.create(application=self.other, user=self.owner, role="owner")
        foreign = self.incident("Foreign outage", app=self.other)
        self.assertEqual(self.client.get(self.url, {"incident": str(foreign.pk)}).status_code, 404)
        self.assertEqual(self.client.get(self.url, {"incident": "bad"}).status_code, 404)
        self.client.force_login(self.admin, backend="django.contrib.auth.backends.ModelBackend")
        self.assertEqual(self.client.get(self.url, {"incident": str(current.pk)}).status_code, 404)

    def test_viewer_can_read_but_flag_and_stale_digest_fail_closed(self):
        current = self.incident("Queue timeout")
        self.client.force_login(self.viewer, backend="django.contrib.auth.backends.ModelBackend")
        self.assertEqual(self.client.get(self.url, {"incident": str(current.pk)}).status_code, 200)
        current.content += " altered"
        current.save(update_fields=["content"])
        self.assertEqual(self.client.get(self.url, {"incident": str(current.pk)}).status_code, 404)
        ApplicationFeature.objects.create(application=self.app, key="service_ops", enabled=False)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_archived_precedent_is_not_returned(self):
        current = self.incident("Queue timeout")
        old = self.incident("Queue timeout", state="Resolved", close_notes="Old fix")
        old.active = False
        old.save(update_fields=["active"])
        with patch("platform_core.graph_ai.graph_citations", return_value=[]):
            response = self.client.get(self.url, {"incident": str(current.pk)})
        self.assertNotContains(response, "Old fix")
        self.assertEqual(KnowledgeEntry.objects.filter(active=True).count(), 1)

    def test_triage_saves_only_verified_hypotheses_and_feedback(self):
        current = self.incident("Queue timeout")
        prior = self.incident(
            "Queue timeout earlier", state="Resolved", close_notes="Restarted worker safely."
        )
        answer = {
            "hypotheses": [
                {
                    "statement": "A worker may be stalled",
                    "next_step": "Inspect queue depth",
                    "citations": [{"id": str(prior.pk), "quote": "Restarted worker safely."}],
                },
                {
                    "statement": "Unsupported claim",
                    "next_step": "Inspect logs",
                    "citations": [{"id": str(prior.pk), "quote": "Made up quote that is absent"}],
                },
            ]
        }
        with (
            patch("platform_core.graph_ai.graph_citations", return_value=[]),
            patch(
                "platform_core.ai.invoke_ai", return_value=__import__("json").dumps(answer)
            ) as ai,
        ):
            response = self.triage(current)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ai.call_count, 1)
        run = TriageRun.objects.get(incident=current)
        self.assertEqual(run.hypotheses.count(), 1)
        self.assertNotEqual(run.band, "high")
        self.assertContains(self.client.get(response["Location"]), "A worker may be stalled")
        hypothesis = TriageHypothesis.objects.get(run=run)
        verdict_url = reverse("serviceops-verdict", args=[self.app.pk, hypothesis.pk])
        self.assertEqual(self.client.post(verdict_url, {"verdict": "accepted"}).status_code, 302)
        self.assertEqual(TriageVerdict.objects.get(hypothesis=hypothesis).verdict, "accepted")
        prior.active = False
        prior.save(update_fields=["active"])
        self.assertNotContains(self.client.get(response["Location"]), "A worker may be stalled")

    def test_viewer_cannot_spend_on_triage_or_save_verdict(self):
        current = self.incident("Queue timeout")
        self.client.force_login(self.viewer, backend="django.contrib.auth.backends.ModelBackend")
        with patch("platform_core.ai.invoke_ai") as ai:
            response = self.triage(current)
        self.assertEqual(response.status_code, 403)
        ai.assert_not_called()

    def test_the_model_gets_room_to_answer(self):
        """A live CareOps triage timed out at 35s and would have been cut at 900 tokens."""
        from platform_core.serviceops_triage import TRIAGE_OUTPUT_LIMIT, TRIAGE_TIMEOUT

        current = self.incident("Queue timeout")
        self.incident(
            "Queue timeout earlier", state="Resolved", close_notes="Restarted worker safely."
        )
        with (
            patch("platform_core.graph_ai.graph_citations", return_value=[]),
            patch("platform_core.ai.invoke_ai", return_value='{"hypotheses": []}') as ai,
        ):
            self.triage(current)
        self.assertEqual(ai.call_args.kwargs["timeout"], TRIAGE_TIMEOUT)
        self.assertEqual(ai.call_args.kwargs["max_tokens"], TRIAGE_OUTPUT_LIMIT)
        self.assertGreaterEqual(TRIAGE_TIMEOUT, 90)
        self.assertGreaterEqual(TRIAGE_OUTPUT_LIMIT, 4096)

    def test_model_outage_preserves_evidence_only_run(self):
        current = self.incident("Queue timeout")
        self.incident(
            "Queue timeout earlier", state="Resolved", close_notes="Restarted worker safely."
        )
        with (
            patch("platform_core.graph_ai.graph_citations", return_value=[]),
            patch(
                "platform_core.ai.invoke_ai", side_effect=ValidationError("Provider unavailable")
            ),
        ):
            response = self.triage(current)
        self.assertEqual(response.status_code, 302)
        run = TriageRun.objects.get(incident=current)
        self.assertEqual(run.status, "evidence")
        self.assertEqual(run.band, "insufficient")
        self.assertContains(self.client.get(response["Location"]), "No supported idea")
        # The failure is the model step's own row in Run details, in the
        # provider's words, and the run still ends as an evidence-only brief.
        details = self.client.get(reverse("serviceops-run", args=[self.app.pk, run.pk]))
        self.assertContains(details, "Provider unavailable")

    def test_weak_related_open_evidence_abstains_without_hypothesis(self):
        current = self.incident("Queue timeout")
        related = self.incident("Queue timeout")
        answer = json.dumps(
            {
                "hypotheses": [
                    {
                        "statement": "Queue worker may be stalled",
                        "next_step": "Inspect queue depth",
                        "citations": [{"id": str(related.pk), "quote": "Queue timeout"}],
                    }
                ]
            }
        )
        with (
            patch("platform_core.graph_ai.graph_citations", return_value=[]),
            patch("platform_core.ai.invoke_ai", return_value=answer),
        ):
            response = self.triage(current)
        self.assertEqual(response.status_code, 302)
        run = TriageRun.objects.get(incident=current)
        self.assertEqual(run.band, "insufficient")
        self.assertEqual(run.hypotheses.count(), 0)

    def test_manual_incident_requires_knowledge_write_and_keeps_header_bounded(self):
        response = self.client.post(
            self.url,
            {
                "action": "manual",
                "title": "Queue alarm",
                "description": "Worker stalled",
                "number": "INC-MANUAL-1",
                "priority": "2",
                "service": "Orders",
            },
        )
        self.assertEqual(response.status_code, 302)
        entry = KnowledgeEntry.objects.get(title="Queue alarm")
        self.assertIn("Type: Incident", entry.content)
        self.assertIn("Number: INC-MANUAL-1", entry.content)
        self.assertEqual(self.client.get(response["Location"]).status_code, 200)
        self.client.force_login(self.viewer, backend="django.contrib.auth.backends.ModelBackend")
        denied = self.client.post(
            self.url,
            {
                "action": "manual",
                "title": "Forbidden",
                "description": "No access",
            },
        )
        self.assertEqual(denied.status_code, 403)
        self.assertFalse(KnowledgeEntry.objects.filter(title="Forbidden").exists())

    def test_same_ci_change_in_prior_window_is_visible(self):
        incident = add_knowledge(
            self.owner,
            self.app.pk,
            "Queue alarm",
            "Queue alarm\n\nType: Incident\nCI: api-1\n"
            "Opened: 2026-09-25 10:00:00\n\nWorker stalled",
            source="https://acme.service-now.com/nav_to.do?uri=incident.do%3Fsys_id%3D1",
        )
        change = add_knowledge(
            self.owner,
            self.app.pk,
            "Deploy worker",
            "Deploy worker\n\nType: Change\nCI: api-1\n"
            "Start: 2026-09-25 08:00:00\n\nUpdated worker config",
            source="https://acme.service-now.com/nav_to.do?uri=change_request.do%3Fsys_id%3D2",
        )
        with patch("platform_core.graph_ai.graph_citations", return_value=[]):
            response = self.client.get(self.url, {"incident": str(incident.pk)})
        self.assertContains(response, "2.0 hours before the incident opened")
        self.assertContains(response, reverse("knowledge-detail", args=[self.app.pk, change.pk]))

    def test_api_requires_bearer_and_application_scoped_access(self):
        incident = self.incident("Queue timeout")
        url = reverse("api-triage", args=[self.app.pk])
        self.assertEqual(
            self.client.post(
                url, json.dumps({"incident_id": str(incident.pk)}), content_type="application/json"
            ).status_code,
            401,
        )
        prefix, secret, digest = issue()
        ApiToken.objects.create(
            application=self.app, user=self.owner, name="triage", prefix=prefix, digest=digest
        )
        with patch("platform_core.graph_ai.graph_citations", return_value=[]):
            response = self.client.post(
                url,
                json.dumps({"incident_id": str(incident.pk)}),
                content_type="application/json",
                headers={"Authorization": f"Bearer {secret}"},
            )
        self.assertEqual(response.status_code, 201)
        run_id = response.json()["id"]
        detail = reverse("api-triage-brief", args=[self.app.pk, run_id])
        self.assertEqual(
            self.client.get(detail, headers={"Authorization": f"Bearer {secret}"}).status_code, 200
        )
        mcp = reverse("api-mcp", args=[self.app.pk])
        with patch("platform_core.ai.invoke_ai") as ai:
            tool = self.client.post(
                mcp,
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "triage_brief", "arguments": {"run_id": run_id}},
                    }
                ),
                content_type="application/json",
                headers={"Authorization": f"Bearer {secret}"},
            )
        self.assertEqual(tool.status_code, 200)
        self.assertEqual(tool.json()["result"]["structuredContent"]["id"], run_id)
        ai.assert_not_called()
        foreign = reverse("api-triage-brief", args=[self.other.pk, run_id])
        self.assertEqual(
            self.client.get(foreign, headers={"Authorization": f"Bearer {secret}"}).status_code, 401
        )

    # ---- the three fixes from the live CareOps walkthrough ----

    def test_english_filler_is_not_a_symptom(self):
        """A live precedent matched on "and, export, for"."""
        from platform_core.serviceops import symptom_terms

        terms = symptom_terms("Export fails for the other clinic", "and it was not working")
        self.assertEqual(terms, {"export", "fail", "clinic"})

    def open_incident(self, title, description, service="CarePath"):
        content = (
            f"{title}\n\nType: Incident\nState: New\nService: {service}\nCI: api-green\n\n"
            f"{description}"
        )
        return add_knowledge(
            self.owner,
            self.app.pk,
            title,
            content,
            source="https://acme.service-now.com/nav_to.do?uri=incident.do%3Fsys_id%3D9",
        )

    def test_a_differently_worded_second_report_is_related(self):
        from platform_core.serviceops import related_open_rows

        current = self.open_incident(
            "FHIR bulk export returning 504 gateway timeout for clinic C",
            "Clinic C export of its patient panel fails with 504 Gateway Timeout.",
        )
        second = self.open_incident(
            "Clinic C export timing out with 504 on large patient panel",
            "Large patient panel export times out with 504 Gateway Timeout.",
        )
        # One shared word is coincidence, another service is another event.
        self.open_incident("Consent refused on export", "Refusals for regranted patients.")
        self.open_incident(
            "Clinic C export gateway timeout", "Patient panel export fails.", service="Billing"
        )
        rows = related_open_rows(self.app, current)
        self.assertEqual([row["entry"] for row in rows], [second])
        self.assertIn("gateway", rows[0]["shared"])

    def test_the_page_and_the_evidence_pack_list_the_same_related_incidents(self):
        from platform_core.serviceops import related_open_rows
        from platform_core.serviceops_triage import evidence_pack

        current = self.open_incident(
            "FHIR bulk export 504 gateway timeout", "Clinic C patient panel export fails."
        )
        self.open_incident(
            "Clinic C export 504 gateway timeout", "Patient panel export times out."
        )
        with patch("platform_core.graph_ai.graph_citations", return_value=[]):
            pack, _ = evidence_pack(self.app, current)
        self.assertEqual(
            {row["id"] for row in pack if row["kind"] == "related_open"},
            {str(row["entry"].pk) for row in related_open_rows(self.app, current)},
        )
        page = self.client.get(self.url, {"incident": str(current.pk)})
        self.assertContains(page, "Shared symptoms:")

    def test_a_citation_is_named_by_its_ticket_not_its_id(self):
        current = self.incident("Queue timeout", number="INC0010009")
        prior = self.incident(
            "Queue timeout earlier",
            number="INC0010002",
            state="Resolved",
            close_notes="Restarted worker safely.",
        )
        run = TriageRun.objects.create(
            application=self.app,
            incident=current,
            requested_by=self.owner,
            status="completed",
            incident_digest=current.digest,
            pack_digest="p",
            evidence=[],
            band="medium",
        )
        TriageHypothesis.objects.create(
            run=run,
            rank=1,
            statement="The worker stalled.",
            next_step="Read the worker log.",
            citations=[{"id": str(prior.pk), "quote": "Queue workers time out."}],
        )
        page = self.client.get(self.url, {"incident": str(current.pk), "run": str(run.pk)})
        self.assertContains(page, "<strong>INC0010002</strong> · Queue timeout earlier", html=True)
        self.assertNotContains(page, f">{prior.pk}</a>")

    # ---- a run in numbered, visible steps ----

    def answer_for(self, prior):
        return json.dumps(
            {
                "hypotheses": [
                    {
                        "statement": "A worker may be stalled",
                        "next_step": "Inspect queue depth",
                        "citations": [{"id": str(prior.pk), "quote": "Restarted worker safely."}],
                    }
                ]
            }
        )

    def test_triage_queues_a_numbered_run_the_page_follows(self):
        from platform_core.serviceops_triage import STEPS

        current = self.incident("Queue timeout")
        response = self.client.post(self.url, {"incident": str(current.pk)})
        run = TriageRun.objects.get(incident=current)
        self.assertEqual((run.number, run.status), (1, "queued"))
        self.assertEqual([step["status"] for step in run.phases], ["pending"] * len(STEPS))
        page = self.client.get(response["Location"])
        self.assertContains(page, "Run #1")
        self.assertContains(page, "data-live-pending")
        self.assertContains(page, "Profile symptoms")

    def test_an_empty_pack_skips_the_model_and_says_nothing_was_charged(self):
        current = self.incident("Queue timeout")
        with (
            patch("platform_core.graph_ai.graph_citations", return_value=[]),
            patch("platform_core.ai.invoke_ai") as ai,
        ):
            response = self.triage(current)
        ai.assert_not_called()
        run = TriageRun.objects.get(incident=current)
        model = next(step for step in run.phases if step["name"] == "model")
        self.assertEqual(model["status"], "skipped")
        self.assertIn("Nothing charged", model["detail"])
        page = self.client.get(response["Location"])
        self.assertNotContains(page, "data-live-pending")
        self.assertContains(page, "No supported idea")

    def test_a_second_run_on_the_same_evidence_reuses_the_answer(self):
        current = self.incident("Queue timeout")
        prior = self.incident(
            "Queue timeout earlier", state="Resolved", close_notes="Restarted worker safely."
        )
        with (
            patch("platform_core.graph_ai.graph_citations", return_value=[]),
            patch("platform_core.ai.invoke_ai", return_value=self.answer_for(prior)) as ai,
        ):
            self.triage(current)
            response = self.triage(current)
        self.assertEqual(ai.call_count, 1)
        second = TriageRun.objects.get(incident=current, number=2)
        model = next(step for step in second.phases if step["name"] == "model")
        self.assertIn("Same evidence as run #1", model["detail"])
        self.assertEqual(second.hypotheses.count(), 1)
        page = self.client.get(response["Location"])
        self.assertContains(page, "All runs (2)")
        details = self.client.get(reverse("serviceops-run", args=[self.app.pk, second.pk]))
        self.assertContains(details, f"Why the evidence strength is {second.band}")
        self.assertContains(details, "Same evidence as run #1")

    def test_a_run_whose_worker_went_away_is_failed_not_left_spinning(self):
        from datetime import timedelta

        from django.utils import timezone

        from platform_core.serviceops_triage import STALL_AFTER, process_next_triage, queue_run

        run = queue_run(self.owner, self.app.pk, self.incident("Queue timeout"))
        long_ago = (timezone.now() - STALL_AFTER - timedelta(minutes=1)).isoformat()
        run.phases[0].update(status="running", started_at=long_ago)
        TriageRun.objects.filter(pk=run.pk).update(status="running", phases=run.phases)
        self.assertTrue(process_next_triage())
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.phases[0]["status"], "failed")
        self.assertIn("server", run.phases[0]["detail"])

    # ---- runs you can find again, and popups ----

    def test_every_run_is_listed_and_opens_on_its_own_page(self):
        from platform_core.serviceops_triage import queue_run

        current = self.incident("Queue timeout", number="INC0010009")
        first = queue_run(self.owner, self.app.pk, current)
        second = queue_run(self.owner, self.app.pk, current)
        listing = self.client.get(reverse("serviceops-runs", args=[self.app.pk]))
        self.assertContains(listing, "#1")
        self.assertContains(listing, "#2")
        self.assertContains(listing, "INC0010009")
        run_url = reverse("serviceops-run", args=[self.app.pk, first.pk])
        self.assertContains(listing, f'href="{run_url}" data-modal')
        self.assertContains(self.client.get(run_url), "Triage run #1")
        # One incident's runs only.
        only = self.client.get(
            reverse("serviceops-runs", args=[self.app.pk]), {"incident": str(current.pk)}
        )
        self.assertContains(only, "Runs for")
        self.assertEqual(second.number, 2)

    def test_a_run_is_not_readable_from_another_application(self):
        from platform_core.serviceops_triage import queue_run

        run = queue_run(self.owner, self.app.pk, self.incident("Queue timeout"))
        ApplicationGrant.objects.create(application=self.other, user=self.owner, role="owner")
        foreign = reverse("serviceops-run", args=[self.other.pk, run.pk])
        self.assertEqual(self.client.get(foreign).status_code, 404)

    def test_an_incident_is_added_in_a_popup_page_of_its_own(self):
        url = reverse("serviceops-incident-new", args=[self.app.pk])
        self.assertContains(self.client.get(url), "Add an incident")
        response = self.client.post(
            url, {"title": "Queue alarm", "description": "Worker stalled", "priority": "2"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("?incident=", response["Location"])
        self.client.force_login(self.viewer, backend="django.contrib.auth.backends.ModelBackend")
        self.assertEqual(self.client.get(url).status_code, 403)

    # ---- a first-time user can follow it ----

    def test_the_list_filters_to_what_needs_attention(self):
        from platform_core.serviceops_triage import queue_run

        waiting = self.incident("Queue timeout", number="INC1", priority="2 - High")
        self.incident("Disk full", number="INC2", priority="4 - Low")
        queue_run(self.owner, self.app.pk, waiting)
        page = self.client.get(self.url, {"show": "untriaged"})
        self.assertContains(page, "INC2")
        self.assertNotContains(page, ">Queue timeout<")
        self.assertContains(page, "Needs triage")
        only_high = self.client.get(self.url, {"priority": "p2"})
        self.assertContains(only_high, "INC1")
        self.assertNotContains(only_high, "Disk full")

    def test_a_newcomer_is_pointed_at_the_guide_and_it_explains_the_steps(self):
        page = self.client.get(self.url)
        guide = reverse("serviceops-guide", args=[self.app.pk])
        self.assertContains(page, "New to ServiceOps?")
        self.assertContains(page, f'href="{guide}" data-modal')
        text = self.client.get(guide)
        for words in ("The six steps", "Ask the AI", "Evidence strength", "never changes"):
            self.assertContains(text, words)

    def test_the_incident_page_speaks_plainly(self):
        current = self.incident("Queue timeout", number="INC0010009")
        page = self.client.get(self.url, {"incident": str(current.pk)})
        self.assertContains(page, "Open in ServiceNow")
        self.assertContains(page, "about 30 seconds and makes one AI call")
        self.assertContains(page, "Component (CI)")
        self.assertNotContains(page, "Signature")

    def test_notes_are_translated_and_ideas_have_headlines(self):
        from platform_core.serviceops_triage import _parsed_hypotheses, plain_limitations

        pack = [{"id": "s1", "excerpt": "Restarted worker safely after the queue stalled."}]
        answer = json.dumps(
            {
                "hypotheses": [
                    {
                        "title": "Worker stalled on the queue",
                        "statement": "The worker may have stalled.",
                        "next_step": "Inspect queue depth",
                        "citations": [{"id": "s1", "quote": "Restarted worker safely"}],
                    }
                ]
            }
        )
        parsed = _parsed_hypotheses(answer, pack)
        self.assertEqual(parsed[0]["title"], "Worker stalled on the queue")
        run = TriageRun(limitations=["Provisional band; no calibrated outcome history", "Other"])
        notes = plain_limitations(run)
        self.assertIn("not calibrated yet", notes[0])
        self.assertEqual(notes[1], "Other")

    def test_an_old_idea_without_a_headline_gets_its_first_sentence(self):
        from platform_core.serviceops import labelled_hypotheses

        current = self.incident("Queue timeout")
        run = TriageRun.objects.create(
            application=self.app,
            incident=current,
            requested_by=self.owner,
            status="completed",
            incident_digest=current.digest,
            pack_digest="p",
        )
        TriageHypothesis.objects.create(
            run=run, rank=1, statement="The worker stalled. It then restarted.", next_step="x"
        )
        self.assertEqual(labelled_hypotheses(self.app, run)[0].headline, "The worker stalled")

    def test_the_cost_note_uses_what_triage_has_really_cost(self):
        from decimal import Decimal

        from platform_core.models import AIUsage
        from platform_core.serviceops import typical_cost

        self.assertIsNone(typical_cost(self.app))
        for amount in ("0.02", "0.04"):
            AIUsage.objects.create(
                application=self.app,
                actor=self.owner,
                purpose="serviceops_triage",
                provider="Anthropic",
                model="claude-sonnet-5",
                request_id=f"r-{amount}",
                input_tokens=1,
                output_tokens=1,
                amount=Decimal(amount),
            )
        self.assertEqual(typical_cost(self.app), "USD 0.03")

    # ---- matching on meaning, not on exact words ----

    def test_phrasings_of_one_fault_become_one_concept(self):
        from platform_core.serviceops import symptom_terms

        self.assertIn("timeout", symptom_terms("Export returns 504", ""))
        self.assertIn("timeout", symptom_terms("Export timed out", ""))
        self.assertIn("timeout", symptom_terms("Gateway time-out on export", ""))
        self.assertIn("auth_failure", symptom_terms("Requests rejected with 401", ""))
        self.assertIn("db_lock", symptom_terms("Search fails: database is locked", ""))
        # Plurals and tenses meet.
        self.assertEqual(
            symptom_terms("Exports refused", "") & symptom_terms("Export refuses", ""),
            {"export", "refus"},
        )

    def test_rare_words_count_more_than_common_ones(self):
        from platform_core.serviceops import similarity, symptom_terms, term_weights
        from platform_core.serviceops_triage import profile

        for title in ("Export slow", "Export failed", "Export stuck", "FHIR export times out"):
            profile(self.open_incident(title, "Clinic reports it."))
        weights = term_weights(self.app)
        current = symptom_terms("FHIR bulk export returns 504", "")
        alike = similarity(current, symptom_terms("FHIR export timed out", ""), weights)
        common = similarity(current, symptom_terms("Export stuck", ""), weights)
        self.assertGreater(alike, 0.35)
        self.assertLess(common, 0.2)

    def test_what_was_reported_outranks_merely_sharing_a_component(self):
        current = self.open_incident(
            "FHIR bulk export returning 504 gateway timeout", "Clinic C export fails."
        )
        timeout = self.incident("Export gateway timeout", state="Resolved", close_notes="Paged it.")
        unrelated = self.incident("Consent refused", state="Resolved", close_notes="Fixed order.")
        from platform_core.serviceops import precedent_rows

        rows = {row["entry"].pk: row for row in precedent_rows(self.app, current)}
        self.assertGreater(rows[timeout.pk]["score"], rows.get(unrelated.pk, {"score": 0})["score"])
        self.assertIn("timeout", " ".join(rows[timeout.pk]["reasons"]))
