"""Onboarding: one list of what an application needs, read by three things.

The point of the module under test is that it is the *only* place these
questions are asked. So these pin the answers, and they pin that the run
narration and the screen both come from here - because the failure mode being
designed out is a screen that says green while a run says no.
"""

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from platform_core.models import (
    AIConfiguration,
    ApplicationFeature,
    ApplicationGrant,
    CodeRepository,
    GraphRevision,
)
from platform_core.readiness import ANALYSIS, DELIVERY, gate_ready, outstanding, steps
from platform_core.workbench import add_knowledge

from . import test_documents

SETTINGS = dict(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    CLAUDE_USE_HOST_LOGIN=False,
)


@override_settings(**SETTINGS)
class ReadinessTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)

    def by_key(self):
        return {step.key: step for step in steps(self.app)}

    def publish(self):
        GraphRevision.objects.create(
            application=self.app,
            number=1,
            fingerprint="f",
            published_at=timezone.now(),
            data={"nodes": [], "edges": [], "sources": []},
            quality={},
        )

    def index(self, name):
        repo = CodeRepository.objects.create(
            application=self.app,
            provider="github",
            external_id=name,
            name=name,
            source_url=f"https://github.com/{name}",
            added_by=self.owner,
            status="ready",
        )
        repo.snapshots.create(number=1, commit_sha="a" * 40)
        return repo

    # ---- the two gates ----

    def test_analysis_and_delivery_are_separate_gates(self):
        """An application that can analyse but not deliver is a real state."""
        self.publish()
        AIConfiguration.objects.create(
            application=self.app,
            purpose="plan_drafting",
            provider="claude",
            model="claude-sonnet-5",
            enabled=True,
            input_rate=1,
            output_rate=5,
            configured_by=self.owner,
        )
        add_knowledge(
            self.owner,
            self.app.pk,
            "OPS-1",
            "A ticket",
            source="https://team.atlassian.net/browse/OPS-1",
        )
        found = steps(self.app)
        self.assertFalse(gate_ready(found, ANALYSIS))  # no credential yet
        self.assertFalse(gate_ready(found, DELIVERY))
        self.assertTrue({step.gate for step in found} == {ANALYSIS, DELIVERY})

    def test_a_missing_published_graph_blocks_analysis(self):
        self.assertFalse(self.by_key()["graph"].ok)
        self.publish()
        self.assertTrue(self.by_key()["graph"].ok)

    # ---- the one that is invisible without being said ----

    def test_two_indexed_repositories_is_reported_as_a_problem(self):
        """A run cannot choose between them, and reads no code at all.

        pin_repository declines to guess, which is right - but the consequence
        is silent until a design comes back naming concepts instead of files.
        """
        self.index("acme/widgets")
        self.assertTrue(self.by_key()["one_repository"].ok)
        self.index("acme/other")
        step = self.by_key()["one_repository"]
        self.assertFalse(step.ok)
        self.assertTrue(step.blocking)
        self.assertIn("no way to choose", step.detail)

    def test_nothing_indexed_is_not_an_ambiguity(self):
        """Zero repositories is one problem, not two: only the first is said."""
        self.assertNotIn("one_repository", self.by_key())

    # ---- a single approver cannot approve anything ----

    @override_settings(ALLOW_SELF_APPROVAL=False)
    def test_one_approver_is_reported_because_they_cannot_approve_their_own_plan(self):
        step = self.by_key()["approver"]
        self.assertFalse(step.ok)
        ApplicationGrant.objects.filter(application=self.app, user=self.viewer).update(
            can_approve=True
        )
        ApplicationGrant.objects.filter(application=self.app, user=self.owner).update(
            can_approve=True
        )
        self.assertTrue(self.by_key()["approver"].ok)

    @override_settings(ALLOW_SELF_APPROVAL=True)
    def test_with_self_approval_one_approver_is_enough_and_no_step_is_shown(self):
        """A second approver is not asked for where one person may approve.

        Nobody holding approval is still reported: no plan could be approved.
        """
        ApplicationGrant.objects.filter(application=self.app).update(can_approve=False)
        step = self.by_key()["approver"]
        self.assertFalse(step.ok)
        self.assertEqual(step.label, "An approver")
        ApplicationGrant.objects.filter(application=self.app, user=self.owner).update(
            can_approve=True
        )
        self.assertNotIn("approver", self.by_key())

    # ---- the host login concession is reported, not hidden ----

    @override_settings(CLAUDE_USE_HOST_LOGIN=True)
    def test_the_host_login_fallback_is_said_out_loud_but_does_not_block(self):
        AIConfiguration.objects.create(
            application=self.app,
            purpose="plan_drafting",
            provider="claude",
            model="claude-sonnet-5",
            enabled=True,
            input_rate=1,
            output_rate=5,
            configured_by=self.owner,
        )
        step = self.by_key()["credential"]
        self.assertTrue(step.ok)
        self.assertFalse(step.blocking)
        self.assertIn("spend whoever started this server", step.detail)

    # ---- every step can be acted on ----

    def test_every_outstanding_step_says_what_to_do_and_where(self):
        """A checklist that only says "no" is a worse version of the error."""
        for step in outstanding(steps(self.app)):
            with self.subTest(step=step.key):
                self.assertTrue(step.action, step.key)
                self.assertTrue(reverse(step.route, args=[self.app.pk]))

    def test_a_modal_step_targets_a_page_that_stands_on_its_own(self):
        """modal.js lifts <main> out of the response, so the target has to be a
        real page that renders and submits by itself - there is no fragment
        mode, and with JavaScript off the link simply navigates."""
        for step in steps(self.app):
            if not step.modal:
                continue
            with self.subTest(step=step.key):
                response = self.client.get(reverse(step.route, args=[self.app.pk]))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "<main")

    def test_the_places_you_go_and_work_are_not_dialogs(self):
        """Knowledge, Connectors and Code Graph are screens, not fields."""
        by_key = self.by_key()
        for key in ("graph", "tickets", "code_graph"):
            with self.subTest(key=key):
                self.assertFalse(by_key[key].modal)

    def test_every_step_says_what_is_the_case_even_when_it_is_fine(self):
        for step in steps(self.app):
            with self.subTest(step=step.key):
                self.assertTrue(step.detail)


def engineering_only(app):
    """An Engineering application: ServiceOps switched off, as the create form does."""
    ApplicationFeature.objects.create(application=app, key="service_ops", enabled=False)


def choose_connectors(app, *kinds):
    """What onboarding's connector question writes: a row for every kind."""
    for kind in ("github", "jira", "servicenow"):
        ApplicationFeature.objects.update_or_create(
            application=app, key=f"connector_{kind}", defaults={"enabled": kind in kinds}
        )


@override_settings(**SETTINGS)
class OnboardingScreenTests(TestCase):
    index = ReadinessTests.index

    def setUp(self):
        ReadinessTests.setUp(self)
        engineering_only(self.app)

    def test_the_screen_lists_both_gates_with_what_to_do(self):
        response = self.client.get(reverse("onboarding", args=[self.app.pk]))
        self.assertContains(response, "Analyse a ticket")
        self.assertContains(response, "Open a pull request")
        self.assertContains(response, "generate a graph and publish it")

    def test_it_is_read_only(self):
        """Each fix has its own permission check and audit event elsewhere."""
        self.assertEqual(
            self.client.post(reverse("onboarding", args=[self.app.pk])).status_code, 405
        )

    def test_another_application_is_not_found(self):
        ApplicationGrant.objects.filter(application=self.app, user=self.owner).delete()
        self.assertEqual(
            self.client.get(reverse("onboarding", args=[self.app.pk])).status_code, 404
        )

    def test_a_brand_new_application_is_welcomed_rather_than_reported_on(self):
        """Nothing done yet is a start, not a list of failures."""
        response = self.client.get(reverse("onboarding", args=[self.app.pk]))
        self.assertContains(response, "is ready to set up")
        self.assertContains(response, "Start with")
        # And it points at one thing rather than leaving eight to choose from.
        self.assertContains(response, "Knowledge graph published")

    def test_the_welcome_gives_way_once_anything_is_configured(self):
        self.index("acme/widgets")
        response = self.client.get(reverse("onboarding", args=[self.app.pk]))
        self.assertNotContains(response, "is ready to set up")
        self.assertContains(response, "Application onboarding")

    def test_progress_is_counted(self):
        response = self.client.get(reverse("onboarding", args=[self.app.pk]))
        # The shared connector question, then Engineering's eight.
        self.assertContains(response, "of 9</strong> done")

    def test_steps_are_numbered_in_one_sequence_across_both_gates(self):
        """The number is what somebody says out loud, so there is only one 7."""
        response = self.client.get(reverse("onboarding", args=[self.app.pk]))
        self.assertContains(response, "1. Connectors chosen")
        self.assertContains(response, "2. Code Factory switched on")
        self.assertContains(response, "6. Tickets imported")
        # First step of the delivery gate, and it is 7 rather than 1 again.
        self.assertContains(response, "7. Code indexed")
        self.assertNotContains(response, "1. Code indexed")

    def test_an_engineering_application_is_not_shown_the_operations_gate(self):
        response = self.client.get(reverse("onboarding", args=[self.app.pk]))
        self.assertNotContains(response, "Triage an incident")

    def test_a_satisfied_step_carries_no_instructions(self):
        """Half the rows are done, and a done row is one line: what it says and
        nothing to do about it. Keeping the advice on them is what made this
        screen four lines per step whether or not anything was outstanding."""
        response = self.client.get(reverse("onboarding", args=[self.app.pk]))
        body = response.content.decode()
        done = body.split('<li class="onboard-step onboard-ok')[1].split("</li>")[0]
        self.assertIn("Code Factory switched on", done)
        self.assertNotIn("onboard-why", done)
        self.assertNotIn('<a class="button', done)


@override_settings(**SETTINGS)
class SetupStripTests(TestCase):
    """The checklist follows you to the screens that satisfy it."""

    publish = ReadinessTests.publish

    def setUp(self):
        ReadinessTests.setUp(self)
        engineering_only(self.app)

    def ready_to_analyse(self):
        from platform_core.models import AIConfiguration

        choose_connectors(self.app, "jira")
        self.publish()
        AIConfiguration.objects.create(
            application=self.app,
            purpose="plan_drafting",
            provider="claude",
            model="claude-sonnet-5",
            enabled=True,
            input_rate=1,
            output_rate=5,
            configured_by=self.owner,
        )
        add_knowledge(
            self.owner,
            self.app.pk,
            "OPS-1",
            "A ticket",
            source="https://team.atlassian.net/browse/OPS-1",
        )

    def test_it_appears_on_a_screen_that_is_not_the_checklist(self):
        response = self.client.get(reverse("documents", args=[self.app.pk]))
        self.assertContains(response, "Setting up")
        self.assertContains(response, "Back to the checklist")

    def test_it_names_the_next_thing_to_do(self):
        response = self.client.get(reverse("documents", args=[self.app.pk]))
        self.assertContains(response, "next: connectors chosen")

    @override_settings(CLAUDE_USE_HOST_LOGIN=True)
    def test_it_goes_once_the_application_can_analyse_a_ticket(self):
        """Not at complete: delivery is a choice, and a banner that can never
        be satisfied stops being read."""
        self.ready_to_analyse()
        response = self.client.get(reverse("documents", args=[self.app.pk]))
        self.assertNotContains(response, "Setting up")
        # Even though delivery is still entirely unconfigured.
        checklist = self.client.get(reverse("onboarding", args=[self.app.pk]))
        self.assertContains(checklist, "Ready to analyse tickets")
        self.assertNotContains(checklist, "Onboarding complete")

    def test_somebody_without_a_grant_is_not_shown_it(self):
        """They cannot act on it, and they should not learn from a banner what
        deny-by-default keeps off the rest of the screen."""
        ApplicationGrant.objects.filter(application=self.app, user=self.owner).update(role="viewer")
        response = self.client.get(reverse("documents", args=[self.app.pk]))
        self.assertContains(response, "Setting up")  # a viewer still holds a grant

        ApplicationGrant.objects.filter(application=self.app, user=self.owner).delete()
        self.assertEqual(self.client.get(reverse("documents", args=[self.app.pk])).status_code, 404)

    def test_the_run_narration_and_the_screen_come_from_the_same_list(self):
        """The drift this module exists to prevent, pinned."""
        from unittest.mock import patch

        from platform_core.code_factory import prevalidate
        from platform_core.models import FactoryRun

        run = FactoryRun.objects.create(
            application=self.app, requested_by=self.owner, ticket_title="A ticket"
        )
        with patch("platform_core.readiness.steps", return_value=[]) as listed:
            prevalidate(run, None)
        listed.assert_called_once_with(self.app)
