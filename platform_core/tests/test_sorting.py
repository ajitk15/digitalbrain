"""Tables sort by their column headings: Documents, Audit log, AI costs, ServiceOps.

Tickets has its own tests in `test_tickets`; these pin the shared helper and
each page that uses it.
"""

import hashlib
import re
from datetime import timedelta
from decimal import Decimal

from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from platform_core import sorting
from platform_core.models import AIUsage, AuditEvent, Document, KnowledgeEntry, User

from . import test_documents, test_serviceops
from .test_ticket_quality import SETTINGS

COLUMNS = {
    "name": sorting.Column("Name", value=lambda row: row["name"] or None),
    "when": sorting.Column("When", "desc", value=lambda row: row["when"]),
}


class HelperTests(SimpleTestCase):
    def request(self, query=""):
        return RequestFactory().get("/list/" + query)

    def test_an_unknown_column_or_direction_is_the_default(self):
        self.assertEqual(sorting.choose(self.request("?sort=x&dir=y"), COLUMNS, "when"),
                         ("when", "desc"))
        self.assertEqual(sorting.choose(self.request("?sort=name"), COLUMNS, "when"),
                         ("name", "asc"))
        self.assertEqual(sorting.choose(self.request(), COLUMNS), (None, ""))

    def test_a_blank_goes_last_either_way(self):
        rows = [{"name": "b"}, {"name": ""}, {"name": "a"}]
        for direction, expected in (("asc", ["a", "b", ""]), ("desc", ["b", "a", ""])):
            ordered = sorting.ordered(rows, COLUMNS["name"], direction)
            self.assertEqual([row["name"] for row in ordered], expected)

    def test_a_heading_keeps_the_filters_drops_the_page_and_turns_round(self):
        found = sorting.headers(self.request("?q=x&page=3&sort=name&dir=asc"), COLUMNS,
                                "name", "asc")
        self.assertEqual(found["name"]["href"], "?q=x&sort=name&dir=desc")
        self.assertEqual(found["name"]["aria_sort"], "ascending")
        self.assertEqual(found["when"]["href"], "?q=x&sort=when&dir=desc")
        self.assertEqual(found["when"]["aria_sort"], "")

    def test_the_default_order_is_not_written_into_page_links(self):
        self.assertEqual(sorting.carried("when", "desc", COLUMNS, "when"), {})
        self.assertEqual(sorting.carried("when", "asc", COLUMNS, "when"),
                         {"sort": "when", "dir": "asc"})
        self.assertEqual(sorting.carried(None, "", COLUMNS), {})


def heading(column, direction):
    return f'<th aria-sort="{direction}"><a class="sort-link is-sorted" href="?sort={column}'


@override_settings(**SETTINGS)
class AuditAndUsageTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        now = timezone.now()
        for hours, name, action in ((3, "zoe", "grant.change"), (2, "adam", "connector.sync"),
                                    (1, "mia", "application.create")):
            actor = User.objects.create_user(name)
            event = AuditEvent.objects.create(actor=actor,
                                              organization_id=self.app.organization_id,
                                              action=action, resource_id=name)
            AuditEvent.objects.filter(pk=event.pk).update(created_at=now - timedelta(hours=hours))
        for model, amount in (("model-b", "0.50"), ("model-a", "2.00"), ("model-c", "0.01")):
            AIUsage.objects.create(application=self.app, actor=self.owner, provider="claude",
                                   model=model, request_id=model, input_tokens=10,
                                   output_tokens=5, amount=Decimal(amount))

    def test_the_audit_log_sorts_by_actor_and_says_so(self):
        body = self.client.get(reverse("audit") + "?sort=actor&dir=asc").content.decode()
        actors = re.findall(r"<td>(adam|mia|zoe)</td>", body)
        self.assertEqual(actors, ["adam", "mia", "zoe"])
        self.assertIn(heading("actor", "ascending"), body)

    def test_the_audit_log_opens_newest_first(self):
        body = self.client.get(reverse("audit")).content.decode()
        self.assertEqual(re.findall(r"<td>(adam|mia|zoe)</td>", body), ["mia", "adam", "zoe"])
        self.assertIn(heading("time", "descending"), body)

    def test_ai_costs_sort_by_cost(self):
        url = reverse("usage", args=[self.app.pk])
        body = self.client.get(url + "?sort=cost&dir=desc").content.decode()
        self.assertEqual(re.findall(r"<small>(model-\w)</small>", body),
                         ["model-a", "model-b", "model-c"])
        body = self.client.get(url + "?sort=model&dir=asc").content.decode()
        self.assertEqual(re.findall(r"<small>(model-\w)</small>", body),
                         ["model-a", "model-b", "model-c"])
        self.assertIn(heading("model", "ascending"), body)


@override_settings(**SETTINGS)
class DocumentsSortTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        for name in ("beta.md", "Alpha.md"):
            Document.objects.create(application=self.app, uploaded_by=self.owner, name=name,
                                    size=1, sha256=hashlib.sha256(name.encode()).hexdigest(),
                                    status="ready", origin="upload")
        KnowledgeEntry.objects.create(application=self.app, author=self.owner,
                                      title="Gamma note", content="Gamma.",
                                      digest=hashlib.sha256(b"Gamma.").hexdigest())
        # Rows made in one test share a timestamp; give each its own.
        now = timezone.now()
        Document.objects.filter(name="beta.md").update(created_at=now - timedelta(hours=2))
        Document.objects.filter(name="Alpha.md").update(created_at=now - timedelta(hours=1))
        KnowledgeEntry.objects.filter(title="Gamma note").update(created_at=now)

    def names(self, query=""):
        body = self.client.get(reverse("documents", args=[self.app.pk]) + query).content
        return re.findall(r"<strong>(Alpha\.md|beta\.md|Gamma note)</strong>",
                          body.decode()), body.decode()

    def test_files_and_records_sort_together_by_name_ignoring_case(self):
        names, body = self.names("?sort=source&dir=asc")
        self.assertEqual(names, ["Alpha.md", "beta.md", "Gamma note"])
        self.assertIn(heading("source", "ascending"), body)
        names, _ = self.names("?sort=source&dir=desc")
        self.assertEqual(names, ["Gamma note", "beta.md", "Alpha.md"])

    def test_newest_first_by_default(self):
        names, body = self.names()
        self.assertEqual(names, ["Gamma note", "Alpha.md", "beta.md"])
        self.assertIn(heading("added", "descending"), body)


@override_settings(**SETTINGS)
class IncidentSortTests(TestCase):
    setUp = test_serviceops.ServiceOpsViewTests.setUp
    incident = test_serviceops.ServiceOpsViewTests.incident

    def test_incidents_sort_by_urgency_and_keep_their_order_until_asked(self):
        self.incident("Disk full", number="INC0010009", priority="4 - Low")
        self.incident("Checkout down", number="INC0010010", priority="1 - Critical")
        self.incident("Slow search", number="INC0010002", priority="")
        url = self.url + "?show=all"
        response = self.client.get(url)
        self.assertEqual([item["number"] for item in response.context["incidents"]],
                         ["INC0010002", "INC0010010", "INC0010009"])
        self.assertNotContains(response, "aria-sort")
        response = self.client.get(url + "&sort=priority&dir=desc")
        self.assertEqual([item["number"] for item in response.context["incidents"]],
                         ["INC0010010", "INC0010009", "INC0010002"])
        self.assertContains(response, '<th aria-sort="descending"><a class="sort-link '
                            'is-sorted" href="?show=all&amp;sort=priority&amp;dir=asc"',
                            html=False)
        response = self.client.get(url + "&sort=incident&dir=asc")
        self.assertEqual([item["number"] for item in response.context["incidents"]],
                         ["INC0010002", "INC0010009", "INC0010010"])
