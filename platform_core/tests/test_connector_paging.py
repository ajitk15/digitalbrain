"""Every connector reads its source to the end, and a schedule reads only what changed.

GitHub issues and the automation sources used to read one page of a hundred,
which `connectors.prune` rightly never took as complete, so a repository past
one page could never be pruned and its older issues never imported. ServiceNow
read one page of any table but incidents, so a knowledge base past a hundred
articles stopped there. And every scheduled import read the whole filter again.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from platform_core import connectors
from platform_core.connector_kinds import (
    MAX_LISTED,
    MAX_PAGES,
    MAX_RECORDS,
    awx_records,
    azure_automation_records,
    github_records,
    jira_records,
    rundeck_records,
    servicenow_records,
    since_jql,
)
from platform_core.models import AuditEvent, Connector, KnowledgeEntry
from platform_core.workbench import add_knowledge

from . import test_documents
from .test_connectors import SETTINGS

JIRA = {"base_url": "https://team.atlassian.net", "auth": "datacenter"}


def query(url):
    return {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}


def issue(number, updated="2026-10-18T10:00:00Z", pull=False):
    found = {"number": number, "title": f"Issue {number}", "updated_at": updated, "body": "b"}
    if pull:
        found["pull_request"] = {}
    return found


class GitHubPagingTests(SimpleTestCase):
    def test_pages_are_read_until_a_short_one(self):
        calls = []

        def answer(url, headers, *, label):
            calls.append(url)
            page = int(query(url)["page"])
            if page < 3:
                # A pull request counts toward the page, though it is not imported.
                start = (page - 1) * MAX_RECORDS
                return [issue(start + n, pull=n == 0) for n in range(MAX_RECORDS)]
            return [issue(1000), issue(1001)]

        with patch("platform_core.connector_kinds.api_json", side_effect=answer):
            records = github_records({"repository": "acme/app"}, "")
        self.assertEqual(len(calls), 3)
        self.assertTrue(records.complete)
        self.assertEqual(len(records), 2 * (MAX_RECORDS - 1) + 2)
        self.assertEqual(query(calls[0])["direction"], "desc")
        self.assertNotIn("since", query(calls[0]))

    def test_an_issue_moved_onto_a_later_page_is_taken_once(self):
        pages = [[issue(n) for n in range(MAX_RECORDS)], [issue(5), issue(500)]]
        with patch("platform_core.connector_kinds.api_json", side_effect=pages):
            records = github_records({"repository": "acme/app"}, "")
        self.assertEqual(len(records), MAX_RECORDS + 1)

    def test_reading_stops_at_the_page_cap_and_says_so(self):
        calls = []

        def answer(url, headers, *, label):
            calls.append(url)
            start = len(calls) * MAX_RECORDS
            return [issue(start + n) for n in range(MAX_RECORDS)]

        with patch("platform_core.connector_kinds.api_json", side_effect=answer):
            records = github_records({"repository": "acme/app"}, "")
        self.assertEqual(len(calls), MAX_PAGES)
        self.assertFalse(records.complete)

    def test_since_a_mark_reads_oldest_first(self):
        calls = []

        def answer(url, headers, *, label):
            calls.append(url)
            return [issue(1, updated="2026-10-18T10:07:00Z")]

        mark = datetime(2026, 10, 18, 11, 2, tzinfo=timezone.get_fixed_timezone(60))
        with patch("platform_core.connector_kinds.api_json", side_effect=answer):
            (record,) = github_records({"repository": "acme/app"}, "", since=mark)
        sent = query(calls[0])
        self.assertEqual((sent["since"], sent["direction"]), ("2026-10-18T10:02:00Z", "asc"))
        self.assertEqual(record.updated, "2026-10-18T10:07:00Z")


class JiraSinceTests(SimpleTestCase):
    NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)

    def test_the_mark_is_a_relative_date_the_account_reads_in_any_time_zone(self):
        since = self.NOW - timedelta(minutes=64, seconds=10)
        self.assertEqual(
            since_jql("order by updated DESC", since, self.NOW),
            'updated >= "-65m" ORDER BY updated ASC',
        )

    def test_the_owners_filter_is_kept_and_its_order_replaced(self):
        since = self.NOW - timedelta(minutes=5)
        self.assertEqual(
            since_jql("project = OPS ORDER BY created DESC", since, self.NOW),
            '(project = OPS) AND updated >= "-5m" ORDER BY updated ASC',
        )

    def test_order_by_inside_a_quoted_value_is_not_an_order(self):
        since = self.NOW - timedelta(minutes=5)
        jql = 'summary ~ "order by date"'
        self.assertEqual(
            since_jql(jql, since, self.NOW),
            f'({jql}) AND updated >= "-5m" ORDER BY updated ASC',
        )

    def test_a_read_since_a_mark_sends_the_narrowed_filter(self):
        calls = []

        def answer(url, headers, *, label):
            calls.append(url)
            return {"issues": [{"key": "OPS-1", "fields": {"updated": "2026-10-18T11:59:00Z"}}],
                    "isLast": True}

        with patch("platform_core.connector_kinds.api_json", side_effect=answer):
            (record,) = jira_records(
                {**JIRA, "jql": "project = OPS"}, "tok",
                since=timezone.now() - timedelta(minutes=30),
            )
        self.assertTrue(query(calls[0])["jql"].startswith('(project = OPS) AND updated >= "-'))
        self.assertEqual(record.updated, "2026-10-18T11:59:00Z")


def snow_rows(start, count):
    return [{"sys_id": f"s{start + n}", "number": f"KB{start + n}", "short_description": "t",
             "text": "body"} for n in range(count)]


class ServiceNowPagingTests(SimpleTestCase):
    CONFIG = {"base_url": "https://acme.service-now.com", "identity": "svc",
              "table": "kb_knowledge"}

    def test_any_table_but_incidents_is_read_to_its_end(self):
        pages = [{"result": snow_rows(0, MAX_RECORDS)}, {"result": snow_rows(MAX_RECORDS, 3)}]
        with patch("platform_core.connector_kinds.api_json", side_effect=pages) as called:
            records = servicenow_records(self.CONFIG, "password")
        self.assertEqual((len(records), called.call_count), (MAX_RECORDS + 3, 2))
        self.assertTrue(records.complete)

    def test_incidents_read_the_depth_the_owner_chose_and_say_whether_it_was_all(self):
        config = {**self.CONFIG, "table": "incident", "history_pages": "1"}
        rows = [{**row, "description": "d"} for row in snow_rows(0, MAX_RECORDS)]
        with patch("platform_core.connector_kinds.api_json", return_value={"result": rows}):
            records = servicenow_records(config, "password")
        self.assertFalse(records.complete)


class AutomationPagingTests(SimpleTestCase):
    def test_awx_asks_for_pages_by_number_and_never_follows_the_next_link(self):
        calls = []

        def answer(url, headers, *, label):
            calls.append(url)
            page = int(query(url)["page"])
            results = [{"id": page * 10 + n, "name": "t"} for n in range(2)]
            following = "https://elsewhere.example/steal" if page < 3 else None
            return {"results": results, "next": following}

        with patch("platform_core.connector_kinds.api_json", side_effect=answer):
            records = awx_records({"base_url": "https://awx.example.com"}, "token")
        self.assertEqual(len(records), 6)
        self.assertTrue(records.complete)
        self.assertTrue(all(url.startswith("https://awx.example.com/api/v2/") for url in calls))

    def test_a_list_handed_over_whole_is_complete_unless_it_was_cut(self):
        config = {"base_url": "https://rundeck.example.com", "project": "ops"}
        jobs = [{"id": f"j{n}", "name": "job"} for n in range(3)]
        with patch("platform_core.connector_kinds.api_json", return_value=jobs):
            self.assertTrue(rundeck_records(config, "tok").complete)
        many = [{"id": f"j{n}", "name": "job"} for n in range(MAX_LISTED + 1)]
        with patch("platform_core.connector_kinds.api_json", return_value=many):
            records = rundeck_records(config, "tok")
        self.assertEqual(len(records), MAX_LISTED)
        self.assertFalse(records.complete)

    def test_azure_with_more_to_give_is_not_complete(self):
        config = {"tenant": "t", "client_id": "c", "subscription": "s",
                  "resource_group": "r", "account": "a"}
        payload = {"value": [{"name": "rb"}], "nextLink": "https://management.azure.com/x"}
        with (
            patch("platform_core.connector_kinds.azure_token", return_value="tok"),
            patch("platform_core.connector_kinds.api_json", return_value=payload) as called,
        ):
            records = azure_automation_records(config, "secret")
        self.assertFalse(records.complete)
        self.assertEqual(called.call_count, 1, "the server's own link is not fetched")


def jira_page(*issues):
    return {"issues": [{"key": key, "fields": {"summary": key, "updated": updated}}
                       for key, updated in issues], "isLast": True}


@override_settings(**SETTINGS)
class HighWaterTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.connector = Connector.objects.create(
            application=self.app, kind="jira", name="Ops board", config=dict(JIRA),
            created_by=self.owner, prune_missing=True, sync_interval_minutes=15,
        )

    def import_(self, page, scheduled=False):
        with (
            patch("platform_core.connectors.credential", return_value="tok"),
            patch("platform_core.connector_kinds.api_json", return_value=page) as called,
        ):
            if scheduled:
                connectors.run_scheduled(Connector.objects.get(pk=self.connector.pk))
            else:
                connectors.sync(self.owner, self.connector.pk, self.app.pk)
        self.connector.refresh_from_db()
        return [query(call.args[0])["jql"] for call in called.call_args_list
                if "/search" in call.args[0]]

    def test_a_full_read_sets_the_mark_and_a_schedule_then_reads_since_it(self):
        self.import_(jira_page(("OPS-1", "2026-10-18T09:00:00.000+0000"),
                               ("OPS-2", "2026-10-18T10:00:00.000+0100")))
        self.assertEqual(self.connector.high_water, datetime(2026, 10, 18, 9, 0, tzinfo=UTC))
        self.assertIsNotNone(self.connector.last_full_read_at)
        Connector.objects.filter(pk=self.connector.pk).update(last_attempt_at=None)
        sent = self.import_(jira_page(("OPS-3", "2026-10-18T09:30:00.000+0000")),
                            scheduled=True)
        self.assertTrue(sent[0].startswith('updated >= "-'), sent)
        self.assertEqual(self.connector.high_water, datetime(2026, 10, 18, 9, 30, tzinfo=UTC))
        audit = AuditEvent.objects.filter(action="connector.synced").order_by("-created_at")[0]
        self.assertTrue(audit.details["since"])

    def test_what_did_not_change_since_the_mark_is_not_retired(self):
        self.import_(jira_page(("OPS-1", "2026-10-18T09:00:00.000+0000"),
                               ("OPS-2", "2026-10-18T09:05:00.000+0000")))
        Connector.objects.filter(pk=self.connector.pk).update(last_attempt_at=None)
        self.import_(jira_page(("OPS-3", "2026-10-18T09:30:00.000+0000")), scheduled=True)
        active = set(KnowledgeEntry.objects.filter(active=True).values_list("source", flat=True))
        self.assertIn("https://team.atlassian.net/browse/OPS-1", active)
        self.assertIn("https://team.atlassian.net/browse/OPS-2", active)

    def test_nothing_changed_since_the_mark_is_not_an_empty_filter(self):
        self.import_(jira_page(("OPS-1", "2026-10-18T09:00:00.000+0000")))
        Connector.objects.filter(pk=self.connector.pk).update(last_attempt_at=None)
        self.import_({"issues": [], "isLast": True}, scheduled=True)
        self.assertEqual((self.connector.last_status, self.connector.last_error), ("ok", ""))
        self.assertEqual(self.connector.high_water, datetime(2026, 10, 18, 9, 0, tzinfo=UTC))

    def test_a_button_press_and_a_day_old_full_read_both_read_everything(self):
        self.import_(jira_page(("OPS-1", "2026-10-18T09:00:00.000+0000")))
        self.assertEqual(self.import_(jira_page())[0], "order by updated DESC")
        Connector.objects.filter(pk=self.connector.pk).update(
            last_attempt_at=None,
            last_full_read_at=timezone.now() - connectors.FULL_READ_EVERY,
        )
        self.assertEqual(self.import_(jira_page(), scheduled=True)[0], "order by updated DESC")

    def test_the_list_says_a_schedule_reads_changes_since_the_mark(self):
        page = self.client.get(reverse("connectors", args=[self.app.pk])).content.decode()
        self.assertNotIn("changes since", page)
        self.import_(jira_page(("OPS-1", "2026-10-18T09:00:00.000+0000")))
        page = self.client.get(reverse("connectors", args=[self.app.pk])).content.decode()
        self.assertIn("changes since", page)

    def test_a_full_read_still_prunes(self):
        add_knowledge(self.owner, self.app.pk, "Gone", "Body",
                      source="https://team.atlassian.net/browse/OPS-99")
        self.import_(jira_page(("OPS-1", "2026-10-18T09:00:00.000+0000")))
        self.assertFalse(KnowledgeEntry.objects.filter(
            source__endswith="/browse/OPS-99", active=True).exists())

    def test_a_new_filter_forgets_the_mark(self):
        self.import_(jira_page(("OPS-1", "2026-10-18T09:00:00.000+0000")))
        response = self.client.post(
            reverse("connector-edit", args=[self.app.pk, self.connector.pk]),
            {"name": "Ops board", "base_url": JIRA["base_url"], "auth": "datacenter",
             "jql": "project = OPS"},
        )
        self.assertEqual(response.status_code, 302)
        self.connector.refresh_from_db()
        self.assertEqual((self.connector.high_water, self.connector.last_full_read_at),
                         (None, None))

    def test_a_kind_that_cannot_read_since_a_mark_always_reads_in_full(self):
        connector = Connector(kind="servicenow", high_water=timezone.now(),
                              last_full_read_at=timezone.now())
        kind = connectors.kind_for("servicenow")
        self.assertIsNone(connectors.changes_since(connector, kind, timezone.now()))
