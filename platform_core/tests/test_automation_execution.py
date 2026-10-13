"""Real local effects, remote adapter contracts, permission and replay boundaries."""

import json
import os
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from platform_core.api_auth import issue
from platform_core.automation_execution import (
    approve_execution,
    enable_local,
    process_next_execution,
    queue_execution,
)
from platform_core.fetching import FetchError
from platform_core.models import (
    ApiToken,
    ApplicationGrant,
    AutomationConnection,
    AutomationRun,
    IncidentProfile,
    KnowledgeEntry,
    OperationsGraph,
    OpsNode,
)
from platform_core.workbench import add_knowledge

from . import test_documents
from .test_browser import BrowserTestCase


class LocalFixture:
    def prepare_local(self):
        self.connection = enable_local(self.owner, self.app.pk)
        self.book = KnowledgeEntry.objects.get(application=self.app,
                                              source="local-runbook:reprofile_incidents")
        self.incident = add_knowledge(self.owner, self.app.pk, "Library checkout timeout",
            "Library checkout timeout\n\nType: Incident\nNumber: INC-LIB-1\nState: New\n"
            "Service: Library\nCI: library-api\n\nCheckout returns 504 gateway timeout.")

    def queue(self, key=None):
        return queue_execution(self.owner, self.app.pk, self.book.pk,
                               self.connection.pk, key or uuid.uuid4(), self.incident.pk)


@override_settings(DOCUMENT_AUTO_CONVERT=False)
class LocalExecutionTests(LocalFixture, TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.prepare_local()

    def test_real_run_changes_profiles_and_verifies_graph(self):
        self.assertFalse(IncidentProfile.objects.exists())
        run = self.queue()
        self.assertTrue(process_next_execution())
        run.refresh_from_db()
        self.assertEqual(run.mode, "real")
        self.assertEqual(run.status, "succeeded")
        self.assertEqual(run.result["verification"], "passed")
        self.assertEqual(run.result["profiles_refreshed"], 1)
        self.assertEqual(IncidentProfile.objects.get(entry=self.incident).source_digest,
                         self.incident.digest)
        graph = OperationsGraph.objects.get(application=self.app)
        self.assertEqual(graph.node_count, OpsNode.objects.filter(application=self.app).count())
        self.assertFalse(OpsNode.objects.filter(application=self.other).exists())
        self.assertEqual(KnowledgeEntry.objects.get(pk=self.incident.pk).digest,
                         self.incident.digest)

    def test_replayed_key_returns_same_run_and_cannot_change_inputs(self):
        key = uuid.uuid4()
        run = self.queue(key)
        self.assertEqual(self.queue(key).pk, run.pk)
        process_next_execution()
        self.assertEqual(self.queue(key).pk, run.pk)
        self.assertEqual(AutomationRun.objects.count(), 1)
        with self.assertRaises(ValidationError):
            queue_execution(self.owner, self.app.pk, self.book.pk, self.connection.pk, key)

    def test_viewer_and_cross_application_requests_do_not_execute(self):
        with self.assertRaises(PermissionDenied):
            queue_execution(self.viewer, self.app.pk, self.book.pk, self.connection.pk,
                            uuid.uuid4())
        self.client.force_login(self.viewer, backend="django.contrib.auth.backends.ModelBackend")
        self.assertEqual(self.client.get(reverse("serviceops-automations",
                                                args=[self.app.pk])).status_code, 403)
        self.assertFalse(AutomationRun.objects.exists())
        ApplicationGrant.objects.create(application=self.other, user=self.owner, role="owner")
        self.client.force_login(self.owner, backend="django.contrib.auth.backends.ModelBackend")
        response = self.client.post(reverse("serviceops-automations", args=[self.other.pk]), {
            "action": "invoke", "automation": str(self.book.pk),
            "connection": str(self.connection.pk), "request_key": str(uuid.uuid4())})
        self.assertEqual(response.status_code, 404)
        self.assertFalse(AutomationRun.objects.exists())

    def test_changed_source_or_connection_blocks_queued_job(self):
        run = self.queue()
        KnowledgeEntry.objects.filter(pk=self.incident.pk).update(content="Changed")
        process_next_execution()
        run.refresh_from_db()
        self.assertEqual(run.status, "blocked")
        self.assertFalse(IncidentProfile.objects.exists())

    def test_disabled_connection_and_revoked_owner_block_job(self):
        run = self.queue()
        self.connection.enabled = False
        self.connection.save(update_fields=["enabled"])
        process_next_execution()
        run.refresh_from_db()
        self.assertEqual(run.status, "blocked")
        enable_local(self.owner, self.app.pk)
        run = self.queue()
        ApplicationGrant.objects.filter(application=self.app, user=self.owner).update(role="viewer")
        process_next_execution()
        run.refresh_from_db()
        self.assertEqual(run.status, "blocked")

    def test_separate_owner_approval_is_required_and_checked_again(self):
        self.connection.require_approval = True
        self.connection.save(update_fields=["require_approval"])
        run = self.queue()
        self.assertEqual(run.status, "pending_approval")
        self.assertFalse(process_next_execution())
        with self.assertRaises(PermissionDenied):
            approve_execution(self.owner, self.app.pk, run.pk)
        ApplicationGrant.objects.filter(application=self.app, user=self.viewer).update(role="owner")
        approve_execution(self.viewer, self.app.pk, run.pk)
        ApplicationGrant.objects.filter(application=self.app, user=self.viewer).update(
            role="viewer")
        process_next_execution()
        run.refresh_from_db()
        self.assertEqual(run.status, "blocked")
        self.assertFalse(IncidentProfile.objects.exists())

    def test_failed_postcondition_rolls_back_local_changes(self):
        run = self.queue()
        with patch("platform_core.ops_graph.records_fingerprint", side_effect=["before", "after"]):
            process_next_execution()
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")
        self.assertFalse(IncidentProfile.objects.exists())
        self.assertFalse(OperationsGraph.objects.exists())

    def test_other_owner_approval_executes_the_real_job(self):
        self.connection.require_approval = True
        self.connection.save(update_fields=["require_approval"])
        run = self.queue()
        ApplicationGrant.objects.filter(application=self.app, user=self.viewer).update(role="owner")
        approve_execution(self.viewer, self.app.pk, run.pk)
        process_next_execution()
        run.refresh_from_db()
        self.assertEqual(run.status, "succeeded")
        self.assertEqual(run.approved_by_id, self.viewer.pk)

    def test_catalog_exposes_scoped_ids_for_machine_invocation(self):
        prefix, token, digest = issue()
        ApiToken.objects.create(application=self.app, user=self.owner, name="catalog",
            prefix=prefix, digest=digest, expires_at=timezone.now() + timedelta(days=1))
        response = self.client.get(reverse("api-automation-catalog", args=[self.app.pk]),
                                   HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["jobs"]), 2)
        self.assertIn(str(self.book.pk), [job["automation_id"] for job in response.json()["jobs"]])

    def test_bearer_api_invokes_and_reads_actual_result_without_cookie_auth(self):
        prefix, token, digest = issue()
        ApiToken.objects.create(application=self.app, user=self.owner, name="test",
            prefix=prefix, digest=digest, expires_at=timezone.now() + timedelta(days=1))
        url = reverse("api-automation-invoke", args=[self.app.pk])
        data = {"automation_id": str(self.book.pk), "connection_id": str(self.connection.pk),
                "request_key": str(uuid.uuid4()), "incident_id": str(self.incident.pk)}
        self.assertEqual(self.client.post(url, data=json.dumps(data),
                                         content_type="application/json").status_code, 401)
        response = self.client.post(url, data=json.dumps(data), content_type="application/json",
                                    HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(response.status_code, 202)
        process_next_execution()
        receipt = self.client.get(reverse("api-automation-run", args=[self.app.pk,
            response.json()["id"]]), HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(receipt.json()["status"], "succeeded")
        self.assertEqual(receipt.json()["result"]["verification"], "passed")


class AWXExecutionTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.connection = AutomationConnection.objects.create(application=self.app,
            created_by=self.owner, provider="awx", name="Test controller",
            base_url="https://awx.example.com", allowed_jobs=["42"], require_approval=False)
        self.book = add_knowledge(self.owner, self.app.pk, "Test AWX job",
            "Test AWX job\n\nType: Automation\nPlatform: AWX\nAutomation ID: 42\n"
            "State: Active\nApproval: none\n\nAn explicitly allowed test template.")

    def queue(self):
        return queue_execution(self.owner, self.app.pk, self.book.pk,
                               self.connection.pk, uuid.uuid4())

    @staticmethod
    def response(data):
        return json.dumps(data).encode(), "application/json", "job.json", "https://awx.example.com"

    def test_launch_once_then_poll_verified_template_with_separate_execute_token(self):
        run = self.queue()
        with (patch("platform_core.automation_adapters.application_secret",
                    return_value="test-execute-token"),
              patch("platform_core.automation_adapters.fetch", side_effect=[
                  self.response({"job": 91}),
                  self.response({"job_template": 42, "status": "successful"})]) as transport):
            process_next_execution()
            run.refresh_from_db()
            self.assertEqual(run.status, "running")
            self.assertEqual(run.external_job_id, "91")
            AutomationRun.objects.filter(pk=run.pk).update(
                last_checked_at=timezone.now() - timedelta(seconds=6))
            process_next_execution()
        run.refresh_from_db()
        self.assertEqual(run.status, "succeeded")
        self.assertEqual(transport.call_args_list[0].kwargs["method"], "POST")
        self.assertEqual(transport.call_args_list[1].kwargs["method"], "GET")
        self.assertEqual(transport.call_args_list[0].args[0],
                         "https://awx.example.com/api/v2/job_templates/42/launch/")
        self.assertNotIn("test-execute-token", json.dumps(run.result) + json.dumps(run.request))

    def test_ambiguous_remote_launch_is_not_retried(self):
        run = self.queue()
        with (patch("platform_core.automation_adapters.application_secret", return_value="test"),
              patch("platform_core.automation_adapters.fetch",
                    side_effect=FetchError("Lost response")) as transport):
            process_next_execution()
            self.assertFalse(process_next_execution())
        run.refresh_from_db()
        self.assertEqual(run.status, "unknown")
        self.assertEqual(transport.call_count, 1)

    def test_absent_execute_credential_refuses_without_network_request(self):
        run = self.queue()
        with (patch("platform_core.automation_adapters.application_secret", return_value=""),
              patch("platform_core.automation_adapters.fetch") as transport):
            process_next_execution()
        transport.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")

    def test_template_not_in_allowlist_cannot_be_queued(self):
        self.connection.allowed_jobs = ["43"]
        self.connection.save(update_fields=["allowed_jobs"])
        with self.assertRaises(ValidationError):
            self.queue()

    def test_invalid_poll_does_not_claim_the_external_job_failed(self):
        run = self.queue()
        with (patch("platform_core.automation_adapters.application_secret", return_value="test"),
              patch("platform_core.automation_adapters.fetch", side_effect=[
                  self.response({"job": 91}),
                  self.response({"job_template": 999, "status": "successful"})])):
            process_next_execution()
            AutomationRun.objects.filter(pk=run.pk).update(
                last_checked_at=timezone.now() - timedelta(seconds=6))
            process_next_execution()
        run.refresh_from_db()
        self.assertEqual(run.status, "unknown")
        self.assertIn("not canceled", run.outcome)


class LocalExecutionBrowserTests(LocalFixture, BrowserTestCase):
    def setUp(self):
        super().setUp()
        self.page.set_viewport_size({"width": 1440, "height": 1000})
        self.prepare_local()

    def test_invoke_real_runbook_and_watch_verified_result(self):
        self.open("serviceops-automations", self.app.pk)
        self.page.get_by_role("button", name="Invoke Re-profile incidents", exact=True).click()
        self.page.locator('[data-live="automation-executions"][data-live-pending]').wait_for()
        self.mark_page()
        self.assertTrue(process_next_execution())
        self.page.locator('[data-live="automation-executions"]:not([data-live-pending])').wait_for()
        self.assertSamePage()
        run = AutomationRun.objects.get(mode="real")
        self.assertEqual(run.status, "succeeded")
        self.assertEqual(run.result["profiles_refreshed"], 1)
        self.assertTrue(IncidentProfile.objects.filter(entry=self.incident).exists())
        self.assertIn("Verified local execution completed", self.page.content())
        self.page.get_by_text("Output and postconditions", exact=True).click()
        self.assertIn("verification", self.page.content())
        if os.environ.get("SERVICEOPS_REAL_SCREENSHOT"):
            self.page.screenshot(path=os.environ["SERVICEOPS_REAL_SCREENSHOT"], full_page=True)
        self.assertNoScriptErrors()
