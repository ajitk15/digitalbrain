"""The Overview: where to start in each application, and what is waiting on you."""

from datetime import timedelta

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from platform_core.models import (
    Application,
    ApplicationFeature,
    ApplicationGrant,
    ChangePlan,
    Connector,
    FactoryRun,
)

from . import test_documents


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class OverviewTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        # Set up already, so the setup prompt does not crowd what is tested.
        Application.objects.filter(pk=self.app.pk).update(setup_completed_at=timezone.now())
        self.url = reverse("dashboard")

    def page(self):
        return self.client.get(self.url)

    def factory_run(self, status, plan_status=None, requested_by=None, finished=None):
        plan = None
        if plan_status:
            plan = ChangePlan.objects.create(
                application=self.app,
                author=self.owner,
                title="Consent",
                proposal="p",
                validation="v",
                digest="d",
                status=plan_status,
            )
        return FactoryRun.objects.create(
            application=self.app,
            requested_by=requested_by or self.owner,
            ticket_external_id="KAN-4",
            status=status,
            plan=plan,
            number=1,
            finished_at=finished,
        )

    def test_each_application_offers_what_it_is_for(self):
        ApplicationFeature.objects.create(application=self.app, key="service_ops", enabled=False)
        body = self.page().content.decode()
        self.assertIn("Analyze a ticket", body)
        self.assertIn(reverse("plans", args=[self.app.pk]), body)
        self.assertNotIn("Triage an incident", body)
        self.assertIn("Ask a question", body)
        ApplicationFeature.objects.filter(application=self.app, key="service_ops").delete()
        for key in ("code_factory", "code_graph"):
            ApplicationFeature.objects.create(application=self.app, key=key, enabled=False)
        body = self.page().content.decode()
        self.assertIn("Triage an incident", body)
        self.assertNotIn("Analyze a ticket", body)
        self.assertIn('<span class="pill">Operations</span>', body)

    def test_nothing_waiting_says_so(self):
        self.assertContains(self.page(), "Nothing is waiting on you")

    def test_a_plan_waiting_for_review_is_shown_to_approvers_only(self):
        self.factory_run("awaiting_review", "pending")
        ApplicationGrant.objects.filter(application=self.app, user=self.owner).update(
            can_approve=True
        )
        self.assertContains(self.page(), "a plan is waiting for your review")
        ApplicationGrant.objects.filter(application=self.app, user=self.owner).update(
            can_approve=False
        )
        self.assertNotContains(self.page(), "a plan is waiting for your review")

    def test_an_approved_run_and_a_recent_failure_are_both_prompts(self):
        self.factory_run("awaiting_review", "approved")
        response = self.page()
        self.assertContains(response, "approved, waiting for its next step")
        FactoryRun.objects.update(status="failed", finished_at=timezone.now())
        response = self.page()
        self.assertContains(response, "KAN-4: failed")
        self.assertContains(response, "attention-problem")

    def test_an_old_failure_is_history_not_a_prompt(self):
        self.factory_run("failed", finished=timezone.now() - timedelta(days=30))
        self.assertNotContains(self.page(), ": failed")

    def test_a_failed_import_is_shown_to_owners_only(self):
        Connector.objects.create(
            application=self.app,
            kind="jira",
            name="Jira",
            config={},
            created_by=self.owner,
            last_status="failed",
        )
        self.assertContains(self.page(), "Jira import failed")
        self.client.force_login(self.viewer, backend="django.contrib.auth.backends.ModelBackend")
        self.assertNotContains(self.page(), "Jira import failed")

    def test_unfinished_setup_points_at_the_checklist(self):
        Application.objects.filter(pk=self.app.pk).update(setup_completed_at=None)
        response = self.page()
        self.assertContains(response, "Continue setup")
        self.assertContains(response, reverse("onboarding", args=[self.app.pk]))

    def test_unassessed_ideas_are_counted_per_incident_latest_run_only(self):
        from platform_core.models import TriageHypothesis, TriageRun
        from platform_core.workbench import add_knowledge

        def incident(number):
            return add_knowledge(
                self.owner,
                self.app.pk,
                f"Incident {number}",
                f"Incident {number}\n\nType: Incident\nNumber: {number}\n\nDown.",
                source=f"https://acme.service-now.com/nav_to.do?uri=incident.do%3Fsys_id%3D{number}",
            )

        def triage(entry, number, ideas):
            run = TriageRun.objects.create(
                application=self.app,
                incident=entry,
                requested_by=self.owner,
                number=number,
                status="completed",
                incident_digest=entry.digest,
                pack_digest="x",
            )
            for rank in range(1, ideas + 1):
                TriageHypothesis.objects.create(
                    run=run, rank=rank, statement="s", next_step="n", citations=[]
                )
            return run

        first, second = incident("INC1"), incident("INC2")
        triage(first, 1, 3)  # replaced by run 2 below
        triage(first, 2, 3)
        triage(second, 3, 2)
        # Three runs and eight ideas, but two incidents waiting.
        self.assertContains(self.page(), "2 triaged incidents with ideas nobody has assessed")

    def test_another_applications_work_never_appears(self):
        ApplicationGrant.objects.create(application=self.other, user=self.viewer, role="owner")
        FactoryRun.objects.create(
            application=self.other,
            requested_by=self.viewer,
            ticket_external_id="SECRET-1",
            status="failed",
            number=1,
            finished_at=timezone.now(),
        )
        self.assertNotContains(self.page(), "SECRET-1")

    def test_the_not_found_page_does_not_claim_access_is_not_the_reason(self):
        """Deny by default answers 404 to someone without a grant, so the page
        must not say that permissions are never why something is missing."""
        response = self.client.get(reverse("application", args=[self.other.pk]))
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "have not been granted", status_code=404)
        self.assertNotContains(response, "that would say 403", status_code=404)
