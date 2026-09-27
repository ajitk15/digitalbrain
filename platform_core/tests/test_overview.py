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
                author=self.viewer,  # somebody else: an author does not review their own
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

    @override_settings(ALLOW_SELF_APPROVAL=False)
    def test_an_author_is_not_asked_to_review_their_own_plan(self):
        """The same rule the approval screen enforces, or the prompt sends an
        author to a review the screen then refuses."""
        ApplicationGrant.objects.filter(application=self.app, user=self.owner).update(
            can_approve=True
        )
        run = self.factory_run("awaiting_review", "pending")
        ChangePlan.objects.filter(pk=run.plan_id).update(author=self.owner)
        self.assertNotContains(self.page(), "a plan is waiting for your review")
        with override_settings(ALLOW_SELF_APPROVAL=True):
            self.assertContains(self.page(), "a plan is waiting for your review")

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

    def test_a_long_list_says_how_long_it_is(self):
        from platform_core.attention import MAX_ITEMS

        for index in range(MAX_ITEMS + 2):
            Connector.objects.create(
                application=self.app,
                kind="jira",
                name=f"Jira {index}",
                config={},
                created_by=self.owner,
                last_status="failed",
            )
        response = self.page()
        self.assertEqual(len(response.context["attention"]), MAX_ITEMS)
        self.assertContains(response, f'<span class="count">{MAX_ITEMS + 2}</span>', html=True)
        self.assertContains(response, f"View all {MAX_ITEMS + 2}</a>")
        self.assertContains(response, f'href="{reverse("attention")}"')

    def test_the_full_list_holds_everything_and_filters_it(self):
        from platform_core.attention import MAX_ITEMS

        for index in range(MAX_ITEMS + 2):
            Connector.objects.create(
                application=self.app,
                kind="jira",
                name=f"Jira {index}",
                config={},
                created_by=self.owner,
                last_status="failed",
            )
        self.factory_run("awaiting_review", "pending")
        ApplicationGrant.objects.filter(application=self.app, user=self.owner).update(
            can_approve=True
        )
        url = reverse("attention")
        everything = self.client.get(url)
        self.assertEqual(len(everything.context["page"].object_list), MAX_ITEMS + 3)
        failures = self.client.get(url, {"kind": "failure"})
        self.assertEqual(len(failures.context["page"].object_list), MAX_ITEMS + 2)
        self.assertNotContains(failures, "waiting for your review")
        reviews = self.client.get(url, {"kind": "review", "application": str(self.app.pk)})
        self.assertContains(reviews, "waiting for your review")
        self.assertEqual(len(reviews.context["page"].object_list), 1)

    def test_the_full_list_never_shows_another_applications_work(self):
        ApplicationGrant.objects.create(application=self.other, user=self.viewer, role="owner")
        FactoryRun.objects.create(
            application=self.other,
            requested_by=self.viewer,
            ticket_external_id="SECRET-1",
            status="failed",
            number=1,
            finished_at=timezone.now(),
        )
        page = self.client.get(reverse("attention"), {"application": str(self.other.pk)})
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "SECRET-1")
        # The sidebar lists it as "No access" for an org admin; the list and
        # its application filter must not offer it at all.
        self.assertNotContains(page, f'value="{self.other.pk}"')

    def test_history_is_never_loaded_to_be_thrown_away(self):
        """Old failures and superseded triage runs are filtered in the database.

        Counted as rows turned into objects while the page renders - post_init
        fires once for each - so a query that fetched history and dropped it in
        Python fails here even when the number of queries stays the same."""
        from django.db import connection
        from django.db.models.signals import post_init
        from django.test.utils import CaptureQueriesContext

        from platform_core.models import TriageRun
        from platform_core.workbench import add_knowledge

        incident = add_knowledge(
            self.owner,
            self.app.pk,
            "Incident INC9",
            "Incident INC9\n\nType: Incident\nNumber: INC9\n\nDown.",
            source="https://acme.service-now.com/nav_to.do?uri=incident.do%3Fsys_id%3DINC9",
        )

        def history(count):
            for _ in range(count):
                number = FactoryRun.objects.count() + 1
                FactoryRun.objects.create(
                    application=self.app,
                    requested_by=self.owner,
                    ticket_external_id=f"OLD-{number}",
                    status="failed",
                    number=number,
                    finished_at=timezone.now() - timedelta(days=60),
                )
                TriageRun.objects.create(
                    application=self.app,
                    incident=incident,
                    requested_by=self.owner,
                    number=TriageRun.objects.count() + 1,
                    status="failed",
                    incident_digest=incident.digest,
                    pack_digest="x",
                )

        def render():
            loaded = []

            def count(sender, **kwargs):
                if sender in (FactoryRun, TriageRun):
                    loaded.append(sender.__name__)

            post_init.connect(count)
            try:
                with CaptureQueriesContext(connection) as captured:
                    self.assertNotContains(self.page(), "OLD-")
            finally:
                post_init.disconnect(count)
            return len(captured), loaded

        history(1)
        queries, loaded = render()
        self.assertEqual(loaded, [])
        history(40)
        self.assertEqual(render(), (queries, []))

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
