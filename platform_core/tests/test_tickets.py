"""Knowledge → Tickets: imported work items listed as tickets, apart from documents."""

import hashlib

from django.test import TestCase, override_settings
from django.urls import reverse

from platform_core.connector_kinds import read_header
from platform_core.graphs import publish_revision, rebuild
from platform_core.models import ApplicationFeature, Connector, GraphRevision, KnowledgeEntry
from platform_core.ticket_library import ticket_connector, ticket_entries

from . import test_documents
from .test_ticket_quality import READY, SETTINGS

JIRA = "https://team.atlassian.net"
SNOW = "https://acme.service-now.com"


def snow(table, sys_id):
    return f"{SNOW}/nav_to.do?uri={table}.do%3Fsys_id%3D{sys_id}"


@override_settings(**SETTINGS)
class TicketsTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)

    def connector(self, kind="jira", name="Jira", app=None, **config):
        defaults = {
            "jira": {"base_url": JIRA, "auth": "cloud"},
            "github": {"repository": "acme/app"},
            "servicenow": {"base_url": SNOW, "table": "incident"},
        }[kind]
        return Connector.objects.create(
            application=app or self.app, kind=kind, name=name, config={**defaults, **config},
        )

    def entry(self, source, content=READY, title="KAN-4 - Patient consent is not checked",
              **fields):
        return KnowledgeEntry.objects.create(
            application=self.app, author=self.owner, title=title, content=content,
            source=source, digest=hashlib.sha256(content.encode()).hexdigest(), **fields,
        )

    def tickets(self, query=""):
        return self.client.get(reverse("tickets", args=[self.app.pk]) + query)

    def documents(self, query=""):
        return self.client.get(reverse("documents", args=[self.app.pk]) + query)

    # -- what is a ticket

    def test_work_item_connectors_are_tickets_and_articles_and_flows_are_not(self):
        cases = [
            ("jira", {}, True),
            ("github", {}, True),
            ("servicenow", {"table": "incident"}, True),
            ("servicenow", {"table": "change_request"}, True),
            ("servicenow", {"table": "problem"}, True),
            ("servicenow", {"table": "kb_knowledge"}, False),
            ("servicenow", {"table": "sys_hub_flow"}, False),
            ("automation_list", {}, False),
        ]
        for kind, config, expected in cases:
            with self.subTest(kind=kind, **config):
                connector = Connector(kind=kind, config=config)
                self.assertEqual(ticket_connector(connector), expected)

    def test_tickets_leave_documents_and_articles_stay(self):
        self.connector()
        self.connector("servicenow", "Incidents")
        self.connector("servicenow", "Articles", table="kb_knowledge")
        self.entry(f"{JIRA}/browse/KAN-4")
        self.entry(snow("incident", "a1"), "Type: Incident\nNumber: INC0010001\nState: New\n\n"
                   "The export is down.", title="INC0010001 - Export down")
        self.entry(snow("kb_knowledge", "k1"), "Type: Knowledge article\nNumber: KB0001\n\n"
                   "Restart the export.", title="KB0001 - Restart the export")
        self.assertEqual(ticket_entries(self.app).count(), 2)

        documents = self.documents()
        self.assertContains(documents, "KB0001 - Restart the export")
        self.assertNotContains(documents, "KAN-4 - Patient consent")
        self.assertNotContains(documents, "INC0010001 - Export down")

        tickets = self.tickets()
        self.assertContains(tickets, "KAN-4 - Patient consent")
        self.assertContains(tickets, "INC0010001 - Export down")
        self.assertNotContains(tickets, "KB0001 - Restart the export")

    def test_a_record_no_connector_claims_stays_on_documents(self):
        self.connector(app=self.other)
        self.entry(f"{JIRA}/browse/KAN-4")
        self.assertContains(self.documents(), "KAN-4 - Patient consent")
        self.assertEqual(ticket_entries(self.app).count(), 0)

    # -- the tab

    def test_no_ticket_connector_no_tab(self):
        self.connector("servicenow", "Articles", table="kb_knowledge")
        response = self.documents()
        self.assertContains(response, ">Documents</a>", html=False)
        self.assertNotContains(response, reverse("tickets", args=[self.app.pk]))

    def test_the_tab_counts_tickets(self):
        self.connector()
        self.entry(f"{JIRA}/browse/KAN-4")
        response = self.documents()
        self.assertContains(
            response, 'Tickets <span class="tab-count">1</span></a>', html=False
        )

    def test_tickets_is_part_of_knowledge_in_the_application_menu(self):
        self.connector()
        response = self.tickets()
        self.assertContains(
            response,
            f'<a href="{reverse("graph", args=[self.app.pk])}" aria-current="page">',
            html=False,
        )
        self.assertContains(
            response,
            f'<a href="{reverse("tickets", args=[self.app.pk])}" aria-current="page">',
            html=False,
        )

    def test_a_documents_search_that_matches_tickets_says_so(self):
        self.connector()
        self.entry(f"{JIRA}/browse/KAN-4")
        response = self.documents("?q=consent")
        self.assertContains(response, "1 ticket also matches")
        self.assertContains(response, reverse("tickets", args=[self.app.pk]) + "?q=consent")

    # -- the page

    def test_a_ticket_reads_as_a_ticket(self):
        self.connector()
        entry = self.entry(f"{JIRA}/browse/KAN-4")
        response = self.tickets()
        self.assertContains(response, '<h2 class="view-title">Tickets</h2>', html=False)
        self.assertContains(response, "<strong>KAN-4</strong>", html=False)
        self.assertContains(response, '<span class="pill">To Do</span>', html=False)
        self.assertContains(response, "<td>High</td>", html=False)
        self.assertContains(response, "<td>Bug</td>", html=False)
        self.assertContains(response, "Oct 03, 2026")
        # The ticket in its own system opens beside this list, not over it.
        self.assertContains(
            response, f'href="{JIRA}/browse/KAN-4" target="_blank" rel="noreferrer noopener"'
        )
        self.assertContains(response, "Jira readiness: <strong>1</strong> ready")
        self.assertContains(
            response,
            reverse("knowledge-detail", args=[self.app.pk, entry.pk]) + "#ticket-quality-detail",
        )

    def test_only_jira_is_scored(self):
        self.connector("servicenow", "Incidents")
        self.entry(snow("incident", "a1"), "Type: Incident\nNumber: INC0010001\nState: New\n\n"
                   "The export is down.", title="INC0010001 - Export down")
        response = self.tickets()
        self.assertContains(response, "<strong>INC0010001</strong>", html=False)
        self.assertContains(response, '<span class="pill">New</span>', html=False)
        self.assertNotContains(response, "<th>Readiness</th>", html=False)
        self.assertNotContains(response, "Jira readiness")

    def test_filters_are_plain_get(self):
        jira = self.connector()
        self.connector("servicenow", "Incidents")
        self.entry(f"{JIRA}/browse/KAN-4")
        self.entry(f"{JIRA}/browse/KAN-5", READY.replace("KAN-4", "KAN-5")
                   .replace("Status: To Do", "Status: Done"), title="KAN-5 - Audit log")
        self.entry(snow("incident", "a1"), "Type: Incident\nNumber: INC0010001\nState: New\n\n"
                   "The export is down.", title="INC0010001 - Export down")

        by_connector = self.tickets(f"?connector={jira.pk}")
        self.assertContains(by_connector, "KAN-5 - Audit log")
        self.assertNotContains(by_connector, "INC0010001 - Export down")

        by_status = self.tickets("?status=Done")
        self.assertContains(by_status, "KAN-5 - Audit log")
        self.assertNotContains(by_status, "KAN-4 - Patient consent")
        self.assertContains(by_status, "Clear filters")
        self.assertContains(by_status, "matching ticket, of 3")

        by_type = self.tickets("?type=Incident")
        self.assertContains(by_type, "INC0010001 - Export down")
        self.assertNotContains(by_type, "KAN-5 - Audit log")

        by_key = self.tickets("?q=kan-5")
        self.assertContains(by_key, "KAN-5 - Audit log")
        self.assertNotContains(by_key, "KAN-4 - Patient consent")

        nothing = self.tickets("?status=Nonsense")
        self.assertContains(nothing, "No ticket matches")

    def test_the_header_is_read_from_the_record_and_not_its_body(self):
        content = "Key: KAN-9\nStatus: Open\n\nNotes follow.\nStatus: Closed"
        self.assertEqual(read_header(content), {"Key": "KAN-9", "Status": "Open"})
        self.assertEqual(read_header("Title\n\nKey: KAN-9\n\nBody"), {"Key": "KAN-9"})
        self.assertEqual(read_header("Plain prose.\n\nMore prose."), {})

    def test_another_applications_tickets_are_not_found(self):
        self.connector(app=self.other)
        response = self.client.get(reverse("tickets", args=[self.other.pk]))
        self.assertEqual(response.status_code, 404)

    def test_restricted_tickets_say_so_and_offer_no_ai_action(self):
        self.connector()
        self.entry(f"{JIRA}/browse/KAN-4", sensitivity="restricted")
        response = self.tickets()
        self.assertContains(response, "Restricted")
        self.assertNotContains(response, "Plan a change")

    def test_a_viewer_is_not_offered_a_plan(self):
        self.connector()
        self.entry(f"{JIRA}/browse/KAN-4")
        self.assertContains(self.tickets(), "Plan a change")
        self.client.force_login(self.viewer,
                                backend="django.contrib.auth.backends.ModelBackend")
        response = self.tickets()
        self.assertContains(response, "KAN-4 - Patient consent")
        self.assertNotContains(response, "Plan a change")

    def test_no_plan_without_code_factory(self):
        self.connector()
        self.entry(f"{JIRA}/browse/KAN-4")
        ApplicationFeature.objects.create(application=self.app, key="code_factory",
                                          enabled=False)
        self.assertNotContains(self.tickets(), "Plan a change")

    def test_only_an_incident_offers_triage(self):
        self.connector()
        self.connector("servicenow", "Incidents")
        self.entry(f"{JIRA}/browse/KAN-4")
        incident = self.entry(snow("incident", "a1"), "Type: Incident\nNumber: INC0010001\n"
                              "State: New\n\nThe export is down.",
                              title="INC0010001 - Export down")
        response = self.tickets()
        self.assertContains(response, "Triage</a>", count=1, html=False)
        self.assertContains(response, str(incident.pk))

    def test_plan_a_change_preselects_the_ticket(self):
        self.connector()
        self.entry(f"{JIRA}/browse/KAN-1", title="KAN-1 - Older")
        entry = self.entry(f"{JIRA}/browse/KAN-4")
        self.entry("", content="Exports must honour consent.", title="Consent rule")
        rebuild(self.app.pk)
        newest = GraphRevision.objects.filter(application=self.app).order_by("-number").first()
        publish_revision(self.owner, self.app.pk, newest.number)
        older = KnowledgeEntry.objects.get(title="KAN-1 - Older")
        response = self.client.get(reverse("plans", args=[self.app.pk]) + f"?ticket={older.pk}")
        self.assertContains(response, f'<option value="{older.pk}" selected>', html=False)
        self.assertNotContains(response, f'<option value="{entry.pk}" selected>', html=False)

    def test_a_ticket_record_goes_back_to_tickets(self):
        self.connector()
        entry = self.entry(f"{JIRA}/browse/KAN-4")
        response = self.client.get(reverse("knowledge-detail", args=[self.app.pk, entry.pk]))
        self.assertContains(response, "Back to tickets")
        self.assertNotContains(response, "Back to documents")
        self.assertContains(
            response,
            f'<a href="{reverse("tickets", args=[self.app.pk])}" aria-current="page">',
            html=False,
        )
