"""The first ServiceOps slice must remain read-only and application-scoped."""

from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from platform_core.connector_kinds import servicenow_records
from platform_core.models import ApplicationFeature, ApplicationGrant, KnowledgeEntry
from platform_core.serviceops import fingerprint, priority_tone
from platform_core.workbench import add_knowledge

from . import test_documents


class FingerprintTests(SimpleTestCase):
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
