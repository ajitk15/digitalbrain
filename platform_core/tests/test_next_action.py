"""A run names the one thing its reader should do next."""

from django.test import TestCase, override_settings
from django.urls import reverse

from platform_core.models import ApplicationGrant, ChangePlan, FactoryRun

from . import test_documents


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    ALLOW_SELF_APPROVAL=False,
)
class NextActionTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        ApplicationGrant.objects.filter(application=self.app, user=self.owner).update(
            can_approve=True
        )

    def make(self, status, plan_status=None, author=None, **extra):
        plan = None
        if plan_status:
            plan = ChangePlan.objects.create(
                application=self.app,
                author=author or self.viewer,
                title="Consent",
                proposal="p",
                validation="v",
                digest="d",
                status=plan_status,
            )
        run = FactoryRun.objects.create(
            application=self.app,
            requested_by=self.owner,
            ticket_external_id="KAN-4",
            status=status,
            plan=plan,
            number=1,
            **extra,
        )
        return self.client.get(reverse("run-detail", args=[self.app.pk, run.pk]))

    def next(self, response):
        return response.context["next"]

    def test_a_pending_plan_asks_its_approver_to_review(self):
        action = self.next(self.make("awaiting_review", "pending"))
        self.assertEqual((action["title"], action["anchor"]), ("Review the plan", "#stage-3"))

    def test_the_author_is_told_a_second_approver_is_needed(self):
        action = self.next(self.make("awaiting_review", "pending", author=self.owner))
        self.assertEqual(action["title"], "Waiting for a second approver")
        self.assertEqual(action["anchor"], "")

    def test_an_approved_plan_asks_for_the_repository_then_the_agents(self):
        action = self.next(self.make("awaiting_review", "approved", proposed_repository="a/b"))
        self.assertEqual(action["title"], "Confirm the repository")
        FactoryRun.objects.update(repository_confirmed=True)
        run = FactoryRun.objects.get()
        action = self.next(self.client.get(reverse("run-detail", args=[self.app.pk, run.pk])))
        self.assertEqual(action["title"], "Run the implementation agents")

    def test_someone_without_approval_is_not_offered_the_step(self):
        ApplicationGrant.objects.filter(application=self.app, user=self.owner).update(
            can_approve=False
        )
        action = self.next(self.make("awaiting_review", "approved"))
        self.assertEqual(action["anchor"], "")

    def test_a_failed_implementation_says_why_and_offers_the_rerun(self):
        response = self.make(
            "failed", "approved", error="Claude did not respond in time.", repository_confirmed=True
        )
        action = self.next(response)
        self.assertEqual(action["tone"], "problem")
        self.assertIn("did not respond in time", action["detail"])
        self.assertEqual(action["button"], "Run the agents again")
        self.assertContains(response, 'role="alert"')

    def test_a_prepared_change_asks_for_the_pull_request_decision(self):
        action = self.next(self.make("prepared", "approved", repository_confirmed=True))
        self.assertEqual(
            (action["title"], action["anchor"]), ("Decide on the pull request", "#stage-5")
        )

    def test_the_summary_is_on_the_page(self):
        self.assertContains(self.make("awaiting_review", "pending"), "Your next action")
