"""Row-level security narrows a request for one application to its own rows.

Phase 5. The database's second layer under the code's own scoping
(`platform_core/tenancy.py`, migration 0091). These run against PostgreSQL
where they can; the routing and coverage checks run everywhere.
"""

import importlib
from contextlib import contextmanager
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import DatabaseError, connection, transaction
from django.test import RequestFactory, TestCase
from django.urls import reverse

from platform_core import tenancy
from platform_core.models import (
    Application,
    ApplicationGrant,
    KnowledgeEntry,
    Organization,
    OrganizationMember,
    Portfolio,
    Product,
)
from platform_core.workbench import add_knowledge

MIGRATION = importlib.import_module("platform_core.migrations.0091_row_security")


def required_application(model):
    for name in ("application", "application_id"):
        try:
            field = model._meta.get_field(name)
        except Exception:
            continue
        return not field.null
    return False


class CoverageTests(TestCase):
    def test_every_table_with_a_required_application_chooses(self):
        models = [
            model
            for model in apps.get_app_config("platform_core").get_models()
            if required_application(model)
        ]
        names = {model.__name__ for model in models}
        self.assertLessEqual(tenancy.UNSCOPED, names, "an unscoped name names no model")
        narrowed = {
            model._meta.db_table for model in models if model.__name__ not in tenancy.UNSCOPED
        }
        self.assertEqual(
            set(MIGRATION.TABLES),
            narrowed,
            "a new table with a required application needs a policy in its migration, "
            "or a place in tenancy.UNSCOPED with the reason",
        )

    def test_the_policies_are_in_place(self):
        if connection.vendor != "postgresql":
            self.skipTest("Row security is PostgreSQL's.")
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT relname FROM pg_class WHERE relname = ANY(%s) "
                "AND relrowsecurity AND relforcerowsecurity",
                [MIGRATION.TABLES],
            )
            secured = {row[0] for row in cursor.fetchall()}
            cursor.execute(
                "SELECT tablename FROM pg_policies WHERE policyname = %s", [MIGRATION.POLICY]
            )
            policies = {row[0] for row in cursor.fetchall()}
        self.assertEqual(secured, set(MIGRATION.TABLES))
        self.assertEqual(policies, set(MIGRATION.TABLES))


class Fixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = get_user_model().objects.create_user("owner", password="not-a-real-password")
        org = Organization.objects.create(name="Northstar")
        OrganizationMember.objects.create(organization=org, user=cls.owner, is_admin=True)
        product = Product.objects.create(
            name="Payments", portfolio=Portfolio.objects.create(name="Commerce", organization=org)
        )
        cls.app = Application.objects.create(name="Refunds", product=product)
        cls.app_b = Application.objects.create(name="Checkout", product=product)
        for app in (cls.app, cls.app_b):
            ApplicationGrant.objects.create(application=app, user=cls.owner, role="owner")
        cls.entry = add_knowledge(cls.owner, cls.app.pk, "Refund window", "Thirty days.")
        cls.entry_b = add_knowledge(cls.owner, cls.app_b.pk, "Checkout limit", "Ten items.")


class RowSecurityTests(Fixture):
    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("Row security is PostgreSQL's; on SQLite the code is the only layer.")
        self.addCleanup(tenancy.clear)

    @contextmanager
    def as_runtime(self):
        """Queries as a role row security applies to.

        A superuser bypasses it, so a suite running as one tests through a
        role of the runtime's shape, made inside the test's own transaction
        and gone with it. A non-superuser owner is held to it already (FORCE).
        """
        with connection.cursor() as cursor:
            cursor.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
            superuser = cursor.fetchone()[0]
            if superuser:
                cursor.execute("CREATE ROLE digitalbrain_rls_probe NOLOGIN")
                cursor.execute(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
                    "TO digitalbrain_rls_probe"
                )
                cursor.execute(
                    "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public "
                    "TO digitalbrain_rls_probe"
                )
                cursor.execute("SET ROLE digitalbrain_rls_probe")
        try:
            yield
        finally:
            if superuser:
                with connection.cursor() as cursor:
                    cursor.execute("RESET ROLE")

    def test_nothing_bound_sees_every_application(self):
        with self.as_runtime():
            self.assertEqual(KnowledgeEntry.objects.count(), 2)

    def test_bound_it_sees_only_its_own_and_another_reads_as_not_found(self):
        with self.as_runtime(), tenancy.scoped(self.app.pk):
            self.assertEqual(list(KnowledgeEntry.objects.values_list("pk", flat=True)),
                             [self.entry.pk])
            # The query that forgot `application=`: not found, not a leak.
            self.assertFalse(KnowledgeEntry.objects.filter(pk=self.entry_b.pk).exists())
            with self.assertRaises(KnowledgeEntry.DoesNotExist):
                KnowledgeEntry.objects.get(pk=self.entry_b.pk)
            # An update aimed at the other application's row touches nothing.
            self.assertEqual(
                KnowledgeEntry.objects.filter(pk=self.entry_b.pk).update(title="Changed"), 0
            )
        self.entry_b.refresh_from_db()
        self.assertEqual(self.entry_b.title, "Checkout limit")
        self.assertIsNone(tenancy.bound())

    def test_a_write_for_another_application_is_refused(self):
        with self.as_runtime(), tenancy.scoped(self.app.pk):
            with self.assertRaisesRegex(DatabaseError, "row-level security"), transaction.atomic():
                KnowledgeEntry.objects.create(
                    application=self.app_b, author=self.owner, title="Planted",
                    content="Planted.", digest="0" * 64,
                )
        self.assertFalse(KnowledgeEntry.objects.filter(title="Planted").exists())

    def test_the_tables_read_across_applications_are_not_narrowed(self):
        with self.as_runtime(), tenancy.scoped(self.app.pk):
            self.assertEqual(ApplicationGrant.objects.filter(user=self.owner).count(), 2)

    def test_scoped_puts_back_what_was_bound(self):
        with tenancy.scoped(self.app.pk):
            with tenancy.scoped(self.app_b.pk):
                self.assertEqual(tenancy.bound(), str(self.app_b.pk))
            self.assertEqual(tenancy.bound(), str(self.app.pk))
        self.assertIsNone(tenancy.bound())
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting(%s, true)", [tenancy.SETTING])
            self.assertEqual(cursor.fetchone()[0], "")


class MiddlewareTests(Fixture):
    def setUp(self):
        self.client.force_login(self.owner, backend="django.contrib.auth.backends.ModelBackend")

    def test_an_application_route_is_bound_to_its_application_and_then_cleared(self):
        with patch.object(tenancy, "bind") as bind, patch.object(tenancy, "clear") as clear:
            self.client.get(reverse("knowledge", args=[self.app.pk]))
        bind.assert_called_once_with(self.app.pk)
        self.assertTrue(clear.called)

    def test_a_route_outside_an_application_is_not_bound(self):
        with patch.object(tenancy, "bind") as bind:
            self.client.get(reverse("dashboard"))
        bind.assert_not_called()

    def test_the_page_still_shows_its_own_rows_and_nothing_is_left_bound(self):
        response = self.client.get(reverse("knowledge", args=[self.app.pk]))
        self.assertContains(response, "Refund window")
        self.assertNotContains(response, "Checkout limit")
        self.assertIsNone(tenancy.bound())
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_setting(%s, true)", [tenancy.SETTING])
                self.assertIn(cursor.fetchone()[0], ("", None))

    def test_a_view_that_fails_is_cleared_too(self):
        def failing(request):
            raise RuntimeError("view failed")

        request = RequestFactory().get(f"/applications/{self.app.pk}/knowledge/")
        with patch.object(tenancy, "clear") as clear, self.assertRaises(RuntimeError):
            tenancy.TenantMiddleware(failing)(request)
        clear.assert_called_once_with()


SEEN = []


def counting_handler(job):
    """A job handler for these tests: what it is bound to, and what it can see."""
    SEEN.append((tenancy.bound(), KnowledgeEntry.objects.count()))


class BackgroundBindingTests(Fixture):
    """A lane chooses across applications, then works bound to the one it took."""

    def setUp(self):
        SEEN.clear()
        self.addCleanup(tenancy.clear)

    def run_job(self, application_id):
        from platform_core import jobs

        kind = jobs.Kind(lane="graph", handler=f"{__name__}.counting_handler")
        with patch.dict(jobs.KINDS, {"probe": kind}):
            jobs.enqueue("probe", "probe", application_id=application_id)
            self.assertTrue(jobs.run_next("graph"))

    def test_a_job_is_bound_to_its_application_and_cleared_after(self):
        with patch.object(tenancy, "bind") as bind:
            self.run_job(self.app.pk)
        bind.assert_called_once_with(self.app.pk)
        self.assertIsNone(tenancy.bound())

    def test_a_job_for_no_application_binds_nothing(self):
        with patch.object(tenancy, "bind") as bind:
            self.run_job(None)
        bind.assert_not_called()
        self.assertEqual(len(SEEN), 1)

    def test_a_code_factory_run_is_worked_bound_to_its_application(self):
        from platform_core.code_factory_analysis import process_next_run
        from platform_core.models import FactoryRun

        run = FactoryRun.objects.create(
            application=self.app_b, requested_by=self.owner, ticket_external_id="OPS-1",
            status="pending",
        )
        with (
            patch.object(tenancy, "bind") as bind,
            patch("platform_core.code_factory_analysis.execute", return_value=True) as execute,
        ):
            self.assertTrue(process_next_run())
        execute.assert_called_once()
        bind.assert_called_once_with(run.application_id)

    def test_on_postgresql_the_job_sees_only_its_own_applications_rows(self):
        if connection.vendor != "postgresql":
            self.skipTest("Row security is PostgreSQL's.")
        with RowSecurityTests.as_runtime(self):
            self.run_job(self.app.pk)
            self.run_job(None)
        self.assertEqual(SEEN, [(str(self.app.pk), 1), (None, 2)])
