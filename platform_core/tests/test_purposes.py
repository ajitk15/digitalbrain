"""What an application is for decides its onboarding, and its connectors are chosen."""

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from platform_core.models import (
    AIConfiguration,
    Application,
    ApplicationFeature,
    ApplicationGrant,
    AuditEvent,
    Connector,
    OrganizationMember,
)
from platform_core.readiness import TRIAGE, all_steps, gate_ready, operations_steps, setup
from platform_core.services import purposes

from . import test_documents
from .test_readiness import SETTINGS, choose_connectors, engineering_only


def operations_only(app):
    for key in ("code_graph", "code_factory"):
        ApplicationFeature.objects.create(application=app, key=key, enabled=False)


@override_settings(**SETTINGS)
class OperationsOnboardingTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        operations_only(self.app)

    def test_purpose_is_read_from_the_switches(self):
        self.assertEqual(purposes(self.app), ["operations"])
        engineering_only(self.other)
        self.assertEqual(purposes(self.other), ["engineering"])

    def test_the_page_shows_the_triage_gate_and_not_code_factory(self):
        response = self.client.get(reverse("onboarding", args=[self.app.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Operations · Triage an incident")
        self.assertContains(response, "Incidents imported")
        self.assertNotContains(response, "Analyse a ticket")
        self.assertNotContains(response, "Code Factory switched on")

    def test_a_chosen_kind_that_needs_a_credential_becomes_a_step(self):
        keys = {step.key for step in operations_steps(self.app)}
        self.assertNotIn("credential_servicenow", keys)
        choose_connectors(self.app, "servicenow")
        keys = {step.key for step in operations_steps(self.app)}
        self.assertIn("credential_servicenow", keys)

    @override_settings(CLAUDE_USE_HOST_LOGIN=True)
    def test_ready_once_connectors_incidents_and_a_model_are_there(self):
        from unittest.mock import patch

        from platform_core.workbench import add_knowledge

        self.assertFalse(setup(self.app).analysis_ready)
        choose_connectors(self.app, "servicenow")
        add_knowledge(
            self.owner,
            self.app.pk,
            "Export times out",
            "Export times out\n\nType: Incident\nNumber: INC1\n\n504.",
            source="https://acme.service-now.com/nav_to.do?uri=incident.do%3Fsys_id%3D1",
        )
        AIConfiguration.objects.create(
            application=self.app,
            purpose="serviceops_triage",
            provider="claude",
            model="claude-sonnet-5",
            enabled=True,
            input_rate=3,
            output_rate=15,
            configured_by=self.owner,
        )
        with patch("platform_core.secrets.application_secret", return_value="x"):
            found = all_steps(self.app)
            self.assertTrue(gate_ready(found, TRIAGE), [s.key for s in found if not s.ok])
            self.assertTrue(setup(self.app).analysis_ready)
        # Changes and runbooks are recommended, never blocking.
        by_key = {step.key: step for step in found}
        self.assertFalse(by_key["changes"].blocking)
        self.assertFalse(by_key["runbooks"].blocking)

    def test_every_modal_step_is_a_page_of_its_own(self):
        for step in all_steps(self.app):
            if step.modal:
                with self.subTest(step=step.key):
                    response = self.client.get(reverse(step.route, args=[self.app.pk]))
                    self.assertEqual(response.status_code, 200)
                    self.assertContains(response, "<main")

    def test_both_purposes_show_both_gates_on_one_page(self):
        response = self.client.get(reverse("onboarding", args=[self.other.pk]))
        self.assertEqual(response.status_code, 404)  # no grant on the other app
        ApplicationFeature.objects.filter(application=self.app).delete()
        response = self.client.get(reverse("onboarding", args=[self.app.pk]))
        self.assertContains(response, "Engineering · Analyse a ticket")
        self.assertContains(response, "Operations · Triage an incident")


@override_settings(**SETTINGS)
class ConnectorChoiceTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        operations_only(self.app)
        self.url = reverse("onboarding-connectors", args=[self.app.pk])

    def test_it_starts_with_the_kinds_that_suit_the_purpose(self):
        response = self.client.get(self.url)
        self.assertRegex(response.content.decode(), r'name="connector_servicenow"[^>]*checked')
        self.assertNotRegex(response.content.decode(), r'name="connector_github"[^>]*checked')

    def test_saving_writes_every_kind_and_audits_the_changes(self):
        response = self.client.post(self.url, {"connector_servicenow": "on"})
        self.assertRedirects(
            response, reverse("onboarding", args=[self.app.pk]), fetch_redirect_response=False
        )
        rows = dict(
            ApplicationFeature.objects.filter(
                application=self.app, key__startswith="connector_"
            ).values_list("key", "enabled")
        )
        self.assertEqual(
            rows,
            {"connector_github": False, "connector_jira": False, "connector_servicenow": True},
        )
        # Only what changed from "enabled" is an event; ServiceNow stayed on.
        self.assertEqual(AuditEvent.objects.filter(action="feature.disabled").count(), 2)
        self.assertFalse(AuditEvent.objects.filter(action="feature.enabled").exists())

    def test_it_is_owner_only_and_a_foreign_application_is_not_found(self):
        ApplicationGrant.objects.filter(application=self.app, user=self.owner).update(role="viewer")
        # A member who is not an owner is refused, as on every owner-only screen.
        self.assertEqual(self.client.get(self.url).status_code, 403)
        foreign = reverse("onboarding-connectors", args=[self.other.pk])
        self.assertEqual(self.client.post(foreign, {}).status_code, 404)


@override_settings(**SETTINGS)
class ConnectorKindEnforcementTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        choose_connectors(self.app, "servicenow")

    def connector(self, kind):
        return Connector.objects.create(
            application=self.app,
            kind=kind,
            name=kind,
            config={"base_url": "https://acme.atlassian.net", "auth": "cloud", "jql": ""},
            created_by=self.owner,
        )

    def test_the_chooser_offers_only_chosen_kinds(self):
        response = self.client.get(reverse("connector-new", args=[self.app.pk]))
        self.assertContains(response, "ServiceNow")
        self.assertNotContains(response, "kind=jira")

    def test_a_switched_off_kind_cannot_be_added_or_edited(self):
        response = self.client.get(reverse("connector-new", args=[self.app.pk]), {"kind": "jira"})
        self.assertEqual(response.status_code, 404)
        existing = self.connector("jira")
        response = self.client.get(reverse("connector-edit", args=[self.app.pk, existing.pk]))
        self.assertEqual(response.status_code, 404)

    def test_a_switched_off_kind_does_not_import_and_says_why(self):
        from platform_core.connectors import sync

        existing = self.connector("jira")
        with self.assertRaises(ValidationError):
            sync(self.owner, existing.pk, self.app.pk)
        existing.refresh_from_db()
        self.assertEqual(existing.last_status, "failed")
        self.assertIn("switched off", existing.last_error)

    def test_a_schedule_stops_for_a_switched_off_kind(self):
        from platform_core.connectors import process_next_connector

        existing = self.connector("jira")
        Connector.objects.filter(pk=existing.pk).update(sync_interval_minutes=60)
        self.assertTrue(process_next_connector())
        existing.refresh_from_db()
        self.assertIn("switched off", existing.last_error)

    def test_credentials_offer_only_what_the_application_uses(self):
        from platform_core.secrets import rows

        engineering_only(self.app)
        listed = {row["entry"].key for row in rows(self.app)}
        self.assertIn("servicenow", listed)
        self.assertNotIn("jira", listed)
        self.assertIn("github_write", listed)
        ApplicationFeature.objects.filter(application=self.app, key="service_ops").delete()
        operations_only(self.app)
        self.assertNotIn("github_write", {row["entry"].key for row in rows(self.app)})


@override_settings(**SETTINGS)
class DeleteApplicationTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        OrganizationMember.objects.filter(user=self.owner).update(is_admin=True)

    def fill(self):
        """An application holding a little of everything that PROTECTs it."""
        from platform_core.models import TriageRun
        from platform_core.workbench import add_knowledge

        incident = add_knowledge(
            self.owner,
            self.app.pk,
            "Export times out",
            "Export times out\n\nType: Incident\n\n504.",
            source="https://acme.service-now.com/nav_to.do?uri=incident.do%3Fsys_id%3D1",
        )
        Connector.objects.create(
            application=self.app, kind="servicenow", name="SN", config={}, created_by=self.owner
        )
        return TriageRun.objects.create(
            application=self.app,
            incident=incident,
            requested_by=self.owner,
            number=1,
            status="completed",
            incident_digest=incident.digest,
            pack_digest="x",
        )

    def call(self, name, app=None):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command(
            "delete_application",
            str((app or self.app).pk),
            confirm_name=name,
            actor=self.owner.username,
            stdout=out,
        )
        return out.getvalue()

    def test_a_wrong_name_removes_nothing(self):
        from django.core.management.base import CommandError

        self.fill()
        with self.assertRaises(CommandError):
            self.call("primary")
        self.assertTrue(Application.objects.filter(pk=self.app.pk).exists())

    def test_it_waits_for_a_run_in_flight(self):
        from django.core.management.base import CommandError

        run = self.fill()
        run.status = "running"
        run.save()
        with self.assertRaises(CommandError):
            self.call(self.app.name)

    def test_everything_goes_and_another_application_is_untouched(self):
        import tempfile
        from pathlib import Path

        from platform_core.models import KnowledgeEntry, TriageRun

        self.fill()
        with (
            tempfile.TemporaryDirectory() as directory,
            override_settings(MANAGED_SECRET_DIRECTORY=directory),
        ):
            mine = Path(directory) / f"servicenow_{self.app.pk}"
            theirs = Path(directory) / f"servicenow_{self.other.pk}"
            mine.write_text("x")
            theirs.write_text("y")
            output = self.call(self.app.name)
            self.assertFalse(mine.exists())
            self.assertTrue(theirs.exists())
        self.assertIn("credentials removed: servicenow", output)
        self.assertFalse(Application.objects.filter(pk=self.app.pk).exists())
        self.assertFalse(KnowledgeEntry.objects.filter(application_id=self.app.pk).exists())
        self.assertFalse(TriageRun.objects.filter(application_id=self.app.pk).exists())
        self.assertTrue(Application.objects.filter(pk=self.other.pk).exists())
        self.assertTrue(AuditEvent.objects.filter(action="application.deleted").exists())
