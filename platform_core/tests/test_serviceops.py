"""ServiceOps evidence and triage stay verified and application-scoped."""

import json
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from platform_core.api_auth import issue
from platform_core.connector_kinds import servicenow_records
from platform_core.models import (
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
        self.assertContains(response, "Priority 4 - Low")
        self.assertContains(response, "Test incident")
        self.assertEqual(response.context["incidents"][0]["number"], "INC0010001")
        self.assertEqual(response.context["incidents"][0]["priority_tone"], "p4")
        detail = self.client.get(self.url, {"incident": str(incident.pk)})
        self.assertContains(
            detail,
            '<dd><span class="incident-badge incident-number">INC0010001</span></dd>',
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
        self.assertContains(response, "Similarity is a search aid")

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
            response = self.client.post(self.url, {"incident": str(current.pk)})
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
            response = self.client.post(self.url, {"incident": str(current.pk)})
        self.assertEqual(response.status_code, 403)
        ai.assert_not_called()

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
            response = self.client.post(self.url, {"incident": str(current.pk)})
        self.assertEqual(response.status_code, 302)
        run = TriageRun.objects.get(incident=current)
        self.assertEqual(run.status, "evidence")
        self.assertEqual(run.band, "insufficient")
        self.assertContains(self.client.get(response["Location"]), "Model unavailable")

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
            response = self.client.post(self.url, {"incident": str(current.pk)})
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
        self.assertContains(response, "2.0 hours before opening")
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
