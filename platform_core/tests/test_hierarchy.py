"""A platform administrator disables, enables and deletes each level of the tree.

Disabling is reversible and stops what has not started; deleting is the second
of two steps, needs the name typed, waits for anything in flight, and keeps the
audit trail and the receipts under a tombstone. Nobody else is told any of it
exists, and none of it gives the administrator a way into an application.
"""

import tempfile
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from django.core.exceptions import PermissionDenied
from django.test import TestCase, override_settings
from django.urls import reverse

from platform_core import hierarchy
from platform_core.models import (
    AIUsage,
    Application,
    ApplicationGrant,
    AuditEvent,
    Connector,
    FactoryRun,
    KnowledgeEntry,
    Organization,
    OrganizationMember,
    Portfolio,
    Product,
    TriageRun,
)
from platform_core.policy import LIVE, applications_for

from . import test_documents

SETTINGS = {
    "DOCUMENT_AUTO_CONVERT": False,
    "STORAGES": {
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}
    },
    "PASSWORD_HASHERS": ["django.contrib.auth.hashers.MD5PasswordHasher"],
}


@override_settings(**SETTINGS)
class HierarchyTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.product = self.app.product
        self.portfolio = self.product.portfolio
        self.org = self.portfolio.organization
        # A second organization nothing here may touch.
        elsewhere = Organization.objects.create(name="Elsewhere")
        self.far = Application.objects.create(
            name="Far",
            product=Product.objects.create(
                name="FP", portfolio=Portfolio.objects.create(name="FPo", organization=elsewhere)
            ),
        )
        self.as_admin()

    def as_admin(self):
        self.client.force_login(self.admin, backend="django.contrib.auth.backends.ModelBackend")

    def as_owner(self):
        self.client.force_login(self.owner, backend="django.contrib.auth.backends.ModelBackend")

    def status(self, level, obj, state):
        return self.client.post(reverse("level-status", args=[level, obj.pk]), {"state": state})

    def delete(self, level, obj, typed):
        return self.client.post(reverse("level-delete", args=[level, obj.pk]), {"confirm": typed})

    def incident(self, app=None):
        from platform_core.workbench import add_knowledge

        return add_knowledge(
            self.owner,
            (app or self.app).pk,
            "Export times out",
            "Export times out\n\nType: Incident\n\n504.",
            source="https://acme.service-now.com/nav_to.do?uri=incident.do%3Fsys_id%3D1",
        )

    def factory_run(self, status, app=None):
        return FactoryRun.objects.create(
            application=app or self.app,
            requested_by=self.owner,
            status=status,
            number=FactoryRun.objects.count() + 1,
        )

    def receipt(self, app=None):
        return AIUsage.objects.create(
            application=app or self.app,
            actor=self.owner,
            provider="openai",
            model="gpt",
            request_id=f"r-{AIUsage.objects.count()}",
            input_tokens=1,
            output_tokens=1,
            amount=Decimal("0.01"),
        )

    # ── nobody else ──────────────────────────────────────────────────────────

    def test_anyone_but_a_platform_administrator_is_told_nothing_exists(self):
        # The owner is an organization administrator and holds an owner grant:
        # neither is platform administration.
        for login in (self.as_owner, lambda: self.client.force_login(
            self.viewer, backend="django.contrib.auth.backends.ModelBackend"
        )):
            login()
            structure = self.client.get(reverse("organization-structure", args=[self.org.pk]))
            self.assertEqual(structure.status_code, 404)
            for level, obj in (
                ("organization", self.org),
                ("portfolio", self.portfolio),
                ("product", self.product),
                ("application", self.app),
            ):
                self.assertEqual(self.status(level, obj, "disable").status_code, 404)
                self.assertEqual(self.delete(level, obj, obj.name).status_code, 404)
        self.assertTrue(Application.objects.get(pk=self.app.pk).active)
        self.assertFalse(AuditEvent.objects.filter(action__endswith=".disabled").exists())
        self.assertFalse(AuditEvent.objects.filter(action__endswith=".deleted").exists())

    def test_an_unknown_level_is_not_found(self):
        response = self.client.post(
            reverse("level-status", args=["grant", self.app.pk]), {"state": "disable"}
        )
        self.assertEqual(response.status_code, 404)

    def test_the_structure_page_names_and_never_opens_an_application(self):
        response = self.client.get(reverse("organization-structure", args=[self.org.pk]))
        self.assertContains(response, "Primary")
        self.assertContains(response, 'method="post"')
        self.assertNotContains(response, reverse("application", args=[self.app.pk]))
        # Delete is not offered until a level is disabled.
        self.assertNotContains(response, 'name="confirm"')
        self.status("product", self.product, "disable")
        response = self.client.get(reverse("organization-structure", args=[self.org.pk]))
        self.assertContains(response, reverse("level-delete", args=["product", self.product.pk]))
        self.assertNotContains(
            response, reverse("level-delete", args=["portfolio", self.portfolio.pk])
        )
        self.assertEqual(list(applications_for(self.admin)), [])

    def test_the_console_links_each_organization_to_its_structure(self):
        response = self.client.get(reverse("platform-console"))
        self.assertContains(response, reverse("organization-structure", args=[self.org.pk]))

    def test_the_organization_page_links_there_for_a_platform_administrator_only(self):
        structure = reverse("organization-structure", args=[self.org.pk])
        OrganizationMember.objects.create(organization=self.org, user=self.admin)
        page = reverse("organization", args=[self.org.pk])
        self.assertContains(self.client.get(page), structure)
        # An organization administrator is not a platform administrator.
        self.as_owner()
        self.assertNotContains(self.client.get(page), structure)

    # ── disable and enable ───────────────────────────────────────────────────

    def test_disabling_a_level_hides_everything_under_it_and_enabling_restores_it(self):
        self.as_owner()
        page = reverse("application", args=[self.app.pk])
        self.assertNotEqual(self.client.get(page).status_code, 404)
        self.as_admin()
        for level, obj in (("portfolio", self.portfolio), ("product", self.product)):
            self.assertRedirects(
                self.status(level, obj, "disable"),
                reverse("organization-structure", args=[self.org.pk]),
            )
            self.assertNotIn(self.app, applications_for(self.owner))
            self.as_owner()
            self.assertEqual(self.client.get(page).status_code, 404)
            self.as_admin()
            self.status(level, obj, "enable")
            self.assertIn(self.app, applications_for(self.owner))
        self.assertEqual(
            list(
                AuditEvent.objects.filter(actor=self.admin)
                .order_by("created_at")
                .values_list("action", flat=True)
            ),
            ["portfolio.disabled", "portfolio.enabled", "product.disabled", "product.enabled"],
        )

    def test_a_parent_turned_back_on_restores_what_was_on_and_nothing_more(self):
        self.status("application", self.other, "disable")
        self.status("portfolio", self.portfolio, "disable")
        self.status("portfolio", self.portfolio, "enable")
        live = set(Application.objects.filter(**LIVE).values_list("pk", flat=True))
        self.assertIn(self.app.pk, live)
        self.assertNotIn(self.other.pk, live)

    def test_disabling_fails_queued_work_and_leaves_running_work_to_finish(self):
        queued, waiting = self.factory_run("pending"), self.factory_run("prepare_queued")
        running = self.factory_run("running")
        elsewhere = self.factory_run("pending", app=self.far)
        triage = TriageRun.objects.create(
            application=self.app,
            incident=self.incident(),
            requested_by=self.owner,
            number=1,
            status="queued",
            incident_digest="x",
            pack_digest="x",
        )
        response = self.status("organization", self.org, "disable")
        self.assertRedirects(response, reverse("organization-structure", args=[self.org.pk]))
        for run in (queued, waiting):
            run.refresh_from_db()
            self.assertEqual(run.status, "failed")
            self.assertEqual(run.error, hierarchy.STOPPED)
        for run, status in ((running, "running"), (elsewhere, "pending")):
            run.refresh_from_db()
            self.assertEqual(run.status, status)
        triage.refresh_from_db()
        self.assertEqual(triage.status, "failed")
        event = AuditEvent.objects.get(action="organization.disabled")
        self.assertEqual(event.actor, self.admin)
        self.assertEqual(
            event.details["stopped"], {"Code Factory runs": 2, "triage runs": 1}
        )

    def test_a_disabled_applications_schedule_waits_rather_than_failing(self):
        from platform_core.connectors import due_connectors

        connector = Connector.objects.create(
            application=self.app,
            kind="servicenow",
            name="SN",
            config={},
            created_by=self.owner,
            sync_interval_minutes=60,
        )
        self.assertIn(connector, due_connectors())
        self.status("product", self.product, "disable")
        self.assertNotIn(connector, due_connectors())
        self.status("product", self.product, "enable")
        self.assertIn(connector, due_connectors())

    def test_no_paid_call_is_admitted_for_a_disabled_application(self):
        from platform_core.gateway import try_admit

        self.status("portfolio", self.portfolio, "disable")
        config = SimpleNamespace(provider="openai")
        with self.assertRaises(PermissionDenied):
            try_admit(self.app, config, "chat", self.owner, Decimal("0"))

    def test_the_organization_screen_still_disables_through_the_same_rules(self):
        queued = self.factory_run("pending")
        self.client.post(reverse("organization-status", args=[self.org.pk]), {"state": "disable"})
        queued.refresh_from_db()
        self.assertEqual(queued.status, "failed")
        self.assertFalse(Organization.objects.get(pk=self.org.pk).active)

    # ── delete ───────────────────────────────────────────────────────────────

    def test_delete_needs_the_level_disabled_first(self):
        self.delete("application", self.app, "Primary")
        self.assertTrue(Application.objects.filter(pk=self.app.pk).exists())
        self.assertFalse(AuditEvent.objects.filter(action="application.deleted").exists())

    def test_a_wrong_name_removes_nothing(self):
        self.status("product", self.product, "disable")
        for typed in ("", "product", "Product "):
            self.delete("product", self.product, typed)
        self.assertTrue(Product.objects.filter(pk=self.product.pk).exists())
        self.assertTrue(Application.objects.filter(pk=self.app.pk).exists())
        self.assertFalse(AuditEvent.objects.filter(action="product.deleted").exists())

    def test_delete_waits_for_anything_in_flight(self):
        self.status("application", self.app, "disable")
        self.factory_run("running")
        response = self.client.post(
            reverse("level-delete", args=["application", self.app.pk]),
            {"confirm": "Primary"},
            follow=True,
        )
        self.assertContains(response, "Something is still running here: 1 Code Factory runs")
        self.assertTrue(Application.objects.filter(pk=self.app.pk).exists())

    def test_deleting_an_application_keeps_its_audit_trail_and_receipts(self):
        self.incident()
        Connector.objects.create(
            application=self.app, kind="servicenow", name="SN", config={}, created_by=self.owner
        )
        receipt = self.receipt()
        self.status("application", self.app, "disable")
        with tempfile.TemporaryDirectory() as directory, override_settings(
            MANAGED_SECRET_DIRECTORY=directory
        ):
            mine = Path(directory) / f"servicenow_{self.app.pk}"
            theirs = Path(directory) / f"servicenow_{self.other.pk}"
            mine.write_text("x")
            theirs.write_text("y")
            response = self.delete("application", self.app, "Primary")
            self.assertFalse(mine.exists())
            self.assertTrue(theirs.exists())
        self.assertRedirects(response, reverse("organization-structure", args=[self.org.pk]))
        # Gone to everyone, kept as a row.
        self.assertFalse(Application.objects.filter(pk=self.app.pk).exists())
        tombstone = Application.everything.get(pk=self.app.pk)
        self.assertIsNotNone(tombstone.deleted_at)
        self.assertFalse(tombstone.active)
        self.assertFalse(KnowledgeEntry.objects.filter(application_id=self.app.pk).exists())
        self.assertFalse(Connector.objects.filter(application_id=self.app.pk).exists())
        self.assertFalse(ApplicationGrant.objects.filter(application_id=self.app.pk).exists())
        self.assertTrue(AIUsage.objects.filter(pk=receipt.pk).exists())
        self.assertTrue(AuditEvent.objects.filter(action="application.disabled").exists())
        event = AuditEvent.objects.get(action="application.deleted")
        self.assertEqual(event.actor, self.admin)
        self.assertEqual(event.organization, self.org)
        self.assertEqual(event.details["applications"], [str(self.app.pk)])
        # Its readable address is free again, and nothing else moved.
        self.assertEqual(Application.objects.create(name="Primary", product=self.product).slug,
                         "primary")
        self.assertTrue(Application.objects.get(pk=self.other.pk).active)
        # And there is no way back.
        self.assertEqual(self.status("application", tombstone, "enable").status_code, 404)

    def test_an_application_that_simulated_an_automation_can_be_deleted(self):
        # An automation run PROTECTs the entries it names, so it has to go
        # before them: the first delete on a real ServiceOps application failed.
        from platform_core.models import AutomationRun

        AutomationRun.objects.create(
            application=self.app,
            automation=self.incident(),
            incident=self.incident(),
            requested_by=self.owner,
        )
        self.status("application", self.app, "disable")
        self.delete("application", self.app, "Primary")
        self.assertIsNotNone(Application.everything.get(pk=self.app.pk).deleted_at)
        self.assertFalse(AutomationRun.objects.filter(application_id=self.app.pk).exists())

    def test_nothing_outside_the_delete_order_protects_what_it_deletes(self):
        """A new model that PROTECTs one the delete removes must join the order.

        Otherwise the database refuses the whole delete, and only on an
        application that happens to hold one of its rows.
        """
        from django.apps import apps
        from django.db import models

        from platform_core.demo_reset import ORDER

        removed = {model for model, _ in ORDER} | set(hierarchy.remaining())
        missing = [
            f"{model.__name__}.{field.name} -> {field.related_model.__name__}"
            for model in apps.get_app_config("platform_core").get_models()
            if model not in removed
            for field in model._meta.fields
            if field.is_relation
            and field.remote_field.on_delete is models.PROTECT
            and field.related_model in removed
        ]
        self.assertEqual(missing, [])

    def test_deleting_an_organization_removes_every_level_and_keeps_its_trail(self):
        self.receipt()
        self.status("organization", self.org, "disable")
        response = self.delete("organization", self.org, "Example")
        self.assertRedirects(response, reverse("platform-console"))
        self.assertFalse(Organization.objects.filter(pk=self.org.pk).exists())
        for model, pk in (
            (Organization, self.org.pk),
            (Portfolio, self.portfolio.pk),
            (Product, self.product.pk),
            (Application, self.app.pk),
            (Application, self.other.pk),
        ):
            self.assertIsNotNone(model.everything.get(pk=pk).deleted_at)
        self.assertFalse(OrganizationMember.objects.filter(organization_id=self.org.pk).exists())
        # The trail still names it: a foreign key from a kept record reaches it.
        event = AuditEvent.objects.get(action="organization.deleted")
        self.assertEqual(event.organization.name, "Example")
        self.assertEqual(len(event.details["applications"]), 2)
        self.assertEqual(AIUsage.objects.filter(application_id=self.app.pk).count(), 1)
        self.assertNotContains(self.client.get(reverse("platform-console")), "Example")
        self.assertTrue(Application.objects.filter(pk=self.far.pk, active=True).exists())
        self.assertEqual(
            self.client.get(reverse("organization-structure", args=[self.org.pk])).status_code, 404
        )

    def test_deleting_a_portfolio_takes_its_products_and_applications(self):
        self.status("portfolio", self.portfolio, "disable")
        self.delete("portfolio", self.portfolio, "P")
        self.assertFalse(Product.objects.filter(pk=self.product.pk).exists())
        self.assertFalse(Application.objects.filter(pk__in=[self.app.pk, self.other.pk]).exists())
        self.assertTrue(Organization.objects.filter(pk=self.org.pk).exists())
        self.assertTrue(AuditEvent.objects.filter(action="portfolio.deleted").exists())

    def test_the_operator_command_shares_the_rules_but_not_the_disable_step(self):
        from io import StringIO

        from django.core.management import call_command

        self.receipt()
        out = StringIO()
        call_command(
            "delete_application",
            str(self.app.pk),
            confirm_name="Primary",
            actor=self.owner.username,
            stdout=out,
        )
        self.assertIn("Deleted Primary", out.getvalue())
        self.assertIsNotNone(Application.everything.get(pk=self.app.pk).deleted_at)
        self.assertEqual(AIUsage.objects.filter(application_id=self.app.pk).count(), 1)
