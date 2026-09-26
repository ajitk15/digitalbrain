"""Clearing an organization's work between demonstrations.

A reset removes exactly what a demonstration re-creates on screen - the
knowledge graph, the code graph and Code Factory runs - and keeps what it takes
time to set up: applications, connectors, credentials, sources, settings and
people. It used to delete the applications too, which meant rebuilding every
connector before every telling. These pin both halves, and the fence around it:
who may, where it is offered at all, and what has to be typed.
"""

from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from platform_core import demo_reset
from platform_core.connectors import due_connector
from platform_core.graphs import process_next_graph
from platform_core.models import (
    AIConfiguration,
    Application,
    ApplicationGrant,
    AuditEvent,
    ChangePlan,
    ChatConversation,
    CodeRepository,
    CodeSnapshot,
    Connector,
    Document,
    FactoryRun,
    GraphRevision,
    KnowledgeEntry,
    KnowledgeGraph,
    Organization,
    OrganizationMember,
    Portfolio,
    Product,
    RunEvent,
    User,
)

SETTINGS = dict(
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    ALLOW_DEMO_RESET=True,
)


def fill(app, user):
    """The work a demonstration leaves behind, and the setup it runs on."""
    Connector.objects.create(
        application=app,
        created_by=user,
        kind="jira",
        name="Jira",
        config={"base_url": "https://acme.atlassian.net"},
        sync_interval_minutes=60,
        last_synced_at=timezone.now(),
        last_attempt_at=timezone.now() - timedelta(days=1),
        last_count=2,
        last_status="ok",
    )
    # Two revisions of one imported ticket: both go, the superseded one too.
    for digest, active in (("old", False), ("new", True)):
        KnowledgeEntry.objects.create(
            application=app,
            author=user,
            title="ACME-1",
            content=digest,
            digest=digest,
            active=active,
            source="https://acme.atlassian.net/browse/ACME-1",
        )
    AIConfiguration.objects.create(
        application=app,
        purpose="chat",
        provider="claude",
        model="claude-sonnet-5",
        configured_by=user,
        input_rate=1,
        output_rate=5,
    )
    KnowledgeEntry.objects.create(
        application=app, author=user, title="A source", content="x", digest="d"
    )
    Document.objects.create(application=app, uploaded_by=user, name="a.md", size=1, sha256="s")
    KnowledgeGraph.objects.create(application=app, status="ready")
    GraphRevision.objects.create(application=app, number=1, fingerprint="f")
    repository = CodeRepository.objects.create(
        application=app,
        added_by=user,
        external_id="acme/widgets",
        name="acme/widgets",
        source_url="https://github.com/acme/widgets",
    )
    snapshot = repository.snapshots.create(number=1, commit_sha="c" * 40)
    plan = ChangePlan.objects.create(
        application=app, author=user, title="p", proposal="p", validation="v", digest="d"
    )
    run = FactoryRun.objects.create(
        application=app,
        requested_by=user,
        number=1,
        ticket_title="A ticket",
        plan=plan,
        code_snapshot=snapshot,
    )
    RunEvent.objects.create(run=run, sequence=1, message="Started.")
    return ChatConversation.objects.create(
        application=app, user=user, title="Asked about it", graph_version=1
    )


@override_settings(**SETTINGS)
class DemoResetTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("siteadmin", is_platform_admin=True)
        self.member = User.objects.create_user("member")
        self.org = Organization.objects.create(name="ACME")
        OrganizationMember.objects.create(organization=self.org, user=self.admin, is_admin=True)
        OrganizationMember.objects.create(organization=self.org, user=self.member)
        portfolio = Portfolio.objects.create(organization=self.org, name="Integrated Care")
        product = Product.objects.create(portfolio=portfolio, name="Care Coordination")
        self.app = Application.objects.create(product=product, name="CarePath")
        ApplicationGrant.objects.create(application=self.app, user=self.member, role="owner")
        self.conversation = fill(self.app, self.member)
        AuditEvent.objects.create(
            actor=self.member,
            organization=self.org,
            action="knowledge.created",
            resource_id=str(self.app.pk),
        )
        self.client.force_login(self.admin, backend="django.contrib.auth.backends.ModelBackend")

    # ---- what it removes ----

    def test_it_removes_the_knowledge_graph_the_code_graph_and_runs(self):
        demo_reset.reset(self.admin, self.org, "ACME")
        for model in (
            FactoryRun,
            RunEvent,
            ChangePlan,
            CodeRepository,
            CodeSnapshot,
            GraphRevision,
        ):
            with self.subTest(model=model.__name__):
                self.assertEqual(model.objects.count(), 0)
        graph = KnowledgeGraph.objects.get(application=self.app)
        self.assertEqual((graph.status, graph.data), ("idle", {}))

    def test_the_worker_does_not_rebuild_the_graph_it_cleared(self):
        """It used to: sources with no graph row is exactly what the worker builds,
        so a draft was back within a second and the reset looked like it had failed."""
        demo_reset.reset(self.admin, self.org, "ACME")
        self.assertFalse(process_next_graph())
        self.assertFalse(GraphRevision.objects.exists())

    def test_a_changed_source_brings_the_worker_back(self):
        demo_reset.reset(self.admin, self.org, "ACME")
        KnowledgeEntry.objects.update(digest="changed")
        self.assertTrue(process_next_graph())
        self.assertEqual(GraphRevision.objects.filter(application=self.app).count(), 1)

    def test_it_removes_what_connectors_imported_and_keeps_the_connectors(self):
        demo_reset.reset(self.admin, self.org, "ACME")
        self.assertFalse(KnowledgeEntry.objects.filter(title="ACME-1").exists())
        connector = Connector.objects.get()
        self.assertEqual(
            (connector.config["base_url"], connector.last_synced_at, connector.last_status),
            ("https://acme.atlassian.net", None, ""),
        )

    def test_a_scheduled_connector_does_not_import_straight_after_a_reset(self):
        """Its last attempt was a day ago; left alone it would be due at once."""
        demo_reset.reset(self.admin, self.org, "ACME")
        self.assertIsNone(due_connector())

    def test_an_uploaded_or_hand_added_source_is_not_an_import(self):
        demo_reset.reset(self.admin, self.org, "ACME")
        self.assertTrue(KnowledgeEntry.objects.filter(title="A source").exists())

    def test_the_knowledge_page_offers_to_generate_after_a_reset(self):
        demo_reset.reset(self.admin, self.org, "ACME")
        self.client.force_login(self.member, backend="django.contrib.auth.backends.ModelBackend")
        page = self.client.get(reverse("graph", args=[self.app.pk]))
        self.assertContains(page, "No knowledge graph yet")
        self.assertContains(page, reverse("graph-generate", args=[self.app.pk]))

    # ---- what it keeps ----

    def test_the_setup_a_demonstration_runs_on_is_kept(self):
        """Connectors above all: rebuilding them before every telling was the cost."""
        demo_reset.reset(self.admin, self.org, "ACME")
        for model in (
            Application,
            Product,
            Portfolio,
            ApplicationGrant,
            Connector,
            AIConfiguration,
            KnowledgeEntry,
            Document,
        ):
            with self.subTest(model=model.__name__):
                self.assertEqual(model.objects.count(), 1)

    def test_chat_is_kept_and_unpinned_from_a_removed_graph_version(self):
        demo_reset.reset(self.admin, self.org, "ACME")
        self.conversation.refresh_from_db()
        self.assertIsNone(self.conversation.graph_version)

    def test_the_organization_its_people_and_the_audit_log_survive(self):
        demo_reset.reset(self.admin, self.org, "ACME")
        self.assertTrue(Organization.objects.filter(pk=self.org.pk).exists())
        self.assertEqual(OrganizationMember.objects.filter(organization=self.org).count(), 2)
        self.assertEqual(User.objects.count(), 2)
        self.assertTrue(AuditEvent.objects.filter(resource_id=str(self.app.pk)).exists())
        # And the reset itself is recorded.
        self.assertTrue(AuditEvent.objects.filter(action="organization.reset").exists())

    def test_another_organization_is_untouched(self):
        other = Organization.objects.create(name="Other")
        portfolio = Portfolio.objects.create(organization=other, name="Theirs")
        product = Product.objects.create(portfolio=portfolio, name="Theirs")
        kept = Application.objects.create(product=product, name="Keep me")
        fill(kept, self.member)
        demo_reset.reset(self.admin, self.org, "ACME")
        self.assertTrue(FactoryRun.objects.filter(application=kept).exists())
        self.assertTrue(CodeRepository.objects.filter(application=kept).exists())
        self.assertTrue(GraphRevision.objects.filter(application=kept).exists())

    # ---- the fence ----

    def test_the_name_must_be_typed_exactly(self):
        for typed in ("", "acme", "ACME ", None):
            with self.subTest(typed=typed):
                with self.assertRaises(ValidationError):
                    demo_reset.reset(self.admin, self.org, typed)
        self.assertEqual(FactoryRun.objects.count(), 1)

    def test_only_a_platform_administrator_may(self):
        with self.assertRaises(PermissionDenied):
            demo_reset.reset(self.member, self.org, "ACME")
        self.assertEqual(FactoryRun.objects.count(), 1)

    @override_settings(ALLOW_DEMO_RESET=False)
    def test_it_is_refused_where_it_is_not_enabled(self):
        with self.assertRaises(PermissionDenied):
            demo_reset.reset(self.admin, self.org, "ACME")
        self.assertEqual(FactoryRun.objects.count(), 1)

    @override_settings(ALLOW_DEMO_RESET=False)
    def test_the_console_does_not_offer_it_where_it_is_not_enabled(self):
        page = self.client.get(reverse("platform-console"))
        self.assertNotContains(page, "Reset for a demonstration")

    def test_the_console_says_what_would_go_before_it_goes(self):
        page = self.client.get(reverse("platform-console"))
        self.assertContains(page, "Reset for a demonstration")
        self.assertContains(page, "There is no undo")
        self.assertContains(page, "1 runs")
        self.assertContains(page, "1 code repositories")
        self.assertContains(page, "1 imported records")
        self.assertContains(page, "connectors")

    # ---- through the screen ----

    def test_the_form_clears_it_and_says_what_it_did(self):
        response = self.client.post(
            reverse("organization-reset", args=[self.org.pk]), {"confirm": "ACME"}, follow=True
        )
        self.assertContains(
            response,
            "was reset: 1 run(s), 1 code repository, 1 graph version(s) and 2 imported record(s)",
        )
        self.assertEqual(FactoryRun.objects.count(), 0)
        self.assertEqual(Connector.objects.count(), 1)

    def test_a_mistyped_name_changes_nothing_and_says_so(self):
        response = self.client.post(
            reverse("organization-reset", args=[self.org.pk]), {"confirm": "acme"}, follow=True
        )
        self.assertContains(response, "Type the organization&#x27;s name exactly")
        self.assertEqual(FactoryRun.objects.count(), 1)

    def test_it_cannot_be_reached_by_following_a_link(self):
        """POST only: there is no page to arrive at by accident."""
        self.assertEqual(
            self.client.get(reverse("organization-reset", args=[self.org.pk])).status_code, 405
        )
