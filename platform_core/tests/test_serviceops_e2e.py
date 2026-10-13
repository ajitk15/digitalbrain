"""Real desktop-browser journeys; providers replaced only at the AI boundary."""

import json
import os
from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.urls import reverse

from platform_core.models import (
    AIConfiguration,
    AuditEvent,
    AutomationRun,
    KnowledgeEntry,
    ServiceOpsTestRun,
    TriageHypothesis,
    TriageRun,
    TriageVerdict,
)
from platform_core.serviceops_triage import process_next_triage
from platform_core.workbench import add_knowledge

from .test_browser import BrowserTestCase


class ServiceOpsJourneyTests(BrowserTestCase):
    def setUp(self):
        super().setUp()
        self.page.set_viewport_size({"width": 1440, "height": 1000})
        self.prior = add_knowledge(self.owner, self.app.pk, "Library checkout timeout earlier",
            "Library checkout timeout earlier\n\nType: Incident\nNumber: INC-LIB-OLD\n"
            "State: Resolved\nService: Library\nCI: library-api\n"
            "Close notes: The database connection pool was exhausted.\n\n"
            "Library checkout returns 504 gateway timeout.")
        self.tool = add_knowledge(self.owner, self.app.pk, "Inspect library connections",
            "Inspect library connections\n\nType: Automation\nPlatform: AWX\n"
            "Automation ID: 42\nEndpoint: https://awx.example.com\n"
            "Service: Library\nCI: library-api\nSymptoms: timeout\nRisk: low\n"
            "Approval: required\n\nInspect database connection counters.")
        AIConfiguration.objects.update_or_create(application=self.app, purpose="serviceops_triage",
            defaults={"provider": "claude", "model": "claude-sonnet-5", "enabled": True,
                      "input_rate": 3, "output_rate": 15})
        answer = json.dumps({"hypotheses": [{"title": "Database connections exhausted",
            "statement": "The connection pool may be exhausted again",
            "next_step": "Inspect database connection counters",
            "citations": [{"id": str(self.prior.pk),
                           "quote": "The database connection pool was exhausted."}]}]})
        self.ai_patch = patch("platform_core.ai.invoke_ai", return_value=answer)
        self.ai = self.ai_patch.start()
        self.addCleanup(self.ai_patch.stop)

    def create_in_browser(self, *, empty=False):
        self.open("serviceops-incident-new", self.app.pk)
        self.page.get_by_label("Title").fill("Library checkout timeout today")
        self.page.get_by_label("Description").fill(
            "Library checkout returns 504 gateway timeout.")
        self.page.get_by_label("Number").fill("INC-LIB-NOW")
        self.page.get_by_label("Service").fill("Library")
        self.page.get_by_label("Configuration item").fill("library-api")
        self.page.get_by_role("button", name="Add incident", exact=True).click()
        self.page.wait_for_url("**/?incident=*")
        self.incident = KnowledgeEntry.objects.get(title="Library checkout timeout today")
        if empty:
            KnowledgeEntry.objects.filter(pk__in=[self.prior.pk, self.tool.pk]).update(active=False)

    def triage_in_browser(self):
        self.page.get_by_role("button", name="Triage this incident", exact=True).click()
        self.page.locator('[data-live="triage-run"][data-live-pending]').wait_for()
        self.assertTrue(process_next_triage())
        self.page.locator('[data-live="triage-run"]:not([data-live-pending])').wait_for()
        self.run = TriageRun.objects.get(incident=self.incident)

    def test_complete_incident_triage_assessment_and_automation_journey(self):
        self.create_in_browser()
        self.assertIn("Library checkout timeout earlier", self.page.content())
        self.triage_in_browser()
        self.assertEqual(self.run.status, "completed")
        self.ai.assert_called_once()
        self.assertEqual(self.run.hypotheses.count(), 1)
        self.assertIn("Database connections exhausted", self.page.content())
        self.page.locator("details.idea-assess > summary").click()
        self.page.locator(".verdict-form select[name=verdict]").select_option("accepted")
        self.page.locator(".verdict-form input[name=note]").fill("Verified in the test environment")
        self.page.locator(".verdict-form").get_by_role("button", name="Save", exact=True).click()
        self.page.get_by_text("Verified in the test environment", exact=False).first.wait_for()
        self.assertEqual(TriageVerdict.objects.get().verdict, "accepted")
        with patch("platform_core.fetching.fetch") as fetch:
            self.page.get_by_role("button", name="Simulate run", exact=True).click()
            self.page.get_by_text("nothing was sent", exact=False).first.wait_for()
        fetch.assert_not_called()
        record = AutomationRun.objects.get()
        self.assertEqual(record.mode, "simulated")
        self.assertEqual(record.incident_id, self.incident.pk)
        self.assertIn("/api/v2/job_templates/42/launch/", record.request["address"])
        self.assertTrue(AuditEvent.objects.filter(action="automation.simulated").exists())
        self.open("serviceops-runs", self.app.pk)
        self.assertIn("Library checkout timeout today", self.page.content())
        self.open("serviceops-run", self.app.pk, self.run.pk)
        self.assertIn("How the confidence score was reached", self.page.content())
        self.assertNoScriptErrors()

    def test_provider_outage_keeps_evidence_and_finishes_the_page(self):
        self.ai.side_effect = ValidationError("Test provider unavailable")
        self.create_in_browser()
        self.triage_in_browser()
        self.assertEqual(self.run.status, "evidence")
        self.assertFalse(TriageHypothesis.objects.exists())
        self.assertIn("Library checkout timeout earlier", self.page.content())
        self.open("serviceops-run", self.app.pk, self.run.pk)
        self.assertIn("Test provider unavailable", self.page.content())
        self.assertNoScriptErrors()

    def test_empty_evidence_abstains_without_an_ai_call(self):
        self.create_in_browser(empty=True)
        self.triage_in_browser()
        self.ai.assert_not_called()
        self.assertEqual(self.run.status, "evidence")
        self.assertFalse(TriageHypothesis.objects.exists())
        self.assertNoScriptErrors()

    def test_stale_evidence_hides_the_previously_verified_idea(self):
        self.create_in_browser()
        self.triage_in_browser()
        KnowledgeEntry.objects.filter(pk=self.prior.pk).update(content="Changed source")
        self.page.reload()
        self.assertNotIn("Database connections exhausted", self.page.content())
        self.assertNoScriptErrors()

    def test_viewer_cannot_start_triage_or_automation(self):
        self.create_in_browser()
        self.sign_in(self.viewer)
        self.open("serviceops", self.app.pk, query=f"?incident={self.incident.pk}")
        self.assertEqual(self.page.get_by_role("button", name="Triage this incident").count(), 0)
        self.assertEqual(self.page.get_by_role("button", name="Simulate run").count(), 0)
        self.assertEqual(self.client.post(reverse("serviceops", args=[self.app.pk]),
                                        {"incident": str(self.incident.pk)}).status_code, 403)
        self.assertNoScriptErrors()

    def test_test_workbench_shows_coverage_without_script_errors(self):
        self.open("serviceops-testing", self.app.pk)
        self.assertIn("End-to-end test automation", self.page.content())
        self.assertEqual(self.page.get_by_role("button", name="Run Browser journey").count(), 1)
        self.page.get_by_role("button", name="Run Browser journey").click()
        self.page.get_by_text("Test run queued.", exact=False).wait_for()
        self.assertEqual(ServiceOpsTestRun.objects.get().status, "queued")
        self.mark_page()
        ServiceOpsTestRun.objects.update(status="passed", summary="Browser scenario completed",
            results=[{"test": "Browser journey", "status": "passed", "seconds": 1}])
        self.page.locator('[data-live="serviceops-tests"]:not([data-live-pending])').wait_for()
        self.assertIn("Browser scenario completed", self.page.content())
        self.assertSamePage()
        screenshot = os.environ.get("SERVICEOPS_TEST_SCREENSHOT")
        if screenshot:
            self.page.screenshot(path=str(Path(screenshot)), full_page=True)
        self.assertNoScriptErrors()
