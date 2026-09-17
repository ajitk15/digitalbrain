"""Jira and ServiceNow connectors, and connector management generally.

The security shape that matters here: GitHub's host is hard-coded, but Jira and
ServiceNow take a base URL from the person configuring them. Every outbound call
therefore has to go through the hardened fetcher, or a connector becomes a way to
make the server request whatever it can reach.
"""

import json
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from platform_core.connector_kinds import (
    KINDS,
    JiraForm,
    ServiceNowForm,
    adf_text,
    jira_records,
    servicenow_records,
)
from platform_core.connectors import sync
from platform_core.models import AuditEvent, Connector, KnowledgeEntry
from platform_core.workbench import add_knowledge

from . import test_documents

SETTINGS = dict(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)


class InstanceUrlTests(SimpleTestCase):
    """A base URL is user input, so it is validated like any other link."""

    def test_internal_and_non_https_targets_are_refused(self):
        for value in (
            "http://localhost:8000",
            "https://127.0.0.1",
            "https://169.254.169.254",
            "http://jira.example.com",
            "ftp://jira.example.com",
            "https://localhost",
        ):
            with self.subTest(url=value):
                self.assertFalse(
                    JiraForm({"base_url": value, "auth": "datacenter"}).is_valid(),
                    f"{value} should not be accepted",
                )

    def test_a_real_site_is_accepted_and_normalised(self):
        form = JiraForm(
            {
                "base_url": "https://team.atlassian.net/secure/Dashboard.jspa",
                "auth": "cloud",
                "account_email": "bot@example.com",
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["base_url"], "https://team.atlassian.net")

    def test_jira_cloud_requires_the_email_its_token_belongs_to(self):
        form = JiraForm({"base_url": "https://team.atlassian.net", "auth": "cloud"})
        self.assertFalse(form.is_valid())

    def test_data_centre_needs_no_email(self):
        form = JiraForm({"base_url": "https://jira.example.com", "auth": "datacenter"})
        self.assertTrue(form.is_valid(), form.errors)

    def test_servicenow_table_and_field_names_are_constrained(self):
        base = {
            "base_url": "https://acme.service-now.com",
            "auth": "basic",
            "identity": "svc",
            "title_field": "short_description",
            "body_field": "description",
        }
        self.assertTrue(ServiceNowForm({**base, "table": "incident"}).is_valid())
        for bad in ("incident;drop", "../secret", "Incident", "incident table"):
            with self.subTest(table=bad):
                self.assertFalse(ServiceNowForm({**base, "table": bad}).is_valid())


class AdapterMappingTests(SimpleTestCase):
    """Records map to knowledge without trusting the shape of what came back."""

    def test_jira_flattens_rich_text_and_builds_a_browse_link(self):
        payload = {
            "issues": [
                {
                    "key": "OPS-12",
                    "fields": {
                        "summary": "Queue backlog",
                        "description": {
                            "type": "doc",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "Queue Beta is full."}],
                                }
                            ],
                        },
                    },
                }
            ]
        }
        with patch("platform_core.connector_kinds.api_json", return_value=payload):
            records = jira_records(
                {"base_url": "https://team.atlassian.net", "auth": "datacenter"}, "tok"
            )
        self.assertEqual(records[0].external_id, "OPS-12")
        self.assertEqual(records[0].title, "Queue backlog")
        self.assertIn("Queue Beta is full.", records[0].body)
        self.assertEqual(records[0].url, "https://team.atlassian.net/browse/OPS-12")

    def test_jira_uses_the_search_endpoint_cloud_still_has(self):
        """Jira Cloud removed /rest/api/3/search in 2025 and answers it with 410.
        A live import against Cloud failed outright until this moved."""
        calls = []

        def record(url, headers, *, label):
            calls.append(url)
            return {"issues": []}

        with patch("platform_core.connector_kinds.api_json", side_effect=record):
            jira_records({"base_url": "https://team.atlassian.net", "auth": "datacenter"}, "tok")
        self.assertEqual(len(calls), 1)
        self.assertIn("/rest/api/3/search/jql?", calls[0])

    def test_jira_falls_back_when_an_instance_lacks_that_endpoint(self):
        """Data Center serves the old endpoint and has no /search/jql."""
        calls = []

        def record(url, headers, *, label):
            calls.append(url)
            if "/search/jql" in url:
                gone = ValidationError("Jira: gone")
                gone.http_status = 410
                raise gone
            return {"issues": []}

        with patch("platform_core.connector_kinds.api_json", side_effect=record):
            jira_records({"base_url": "https://team.atlassian.net", "auth": "datacenter"}, "tok")
        self.assertEqual(len(calls), 2)
        self.assertIn("/rest/api/3/search?", calls[1])

    def test_a_rejected_credential_is_reported_rather_than_retried(self):
        """Only "that endpoint is not here" earns a second request. Retrying a
        401 against the removed endpoint would report 410 and hide the real cause."""
        calls = []

        def record(url, headers, *, label):
            calls.append(url)
            refused = ValidationError("Jira: 401")
            refused.http_status = 401
            raise refused

        with patch("platform_core.connector_kinds.api_json", side_effect=record):
            with self.assertRaises(ValidationError):
                jira_records({"base_url": "https://team.atlassian.net", "auth": "cloud",
                              "account_email": "a@b.com"}, "tok")
        self.assertEqual(len(calls), 1)

    def test_deeply_nested_rich_text_terminates(self):
        node = {"type": "text", "text": "deep"}
        for _ in range(40):
            node = {"type": "paragraph", "content": [node]}
        self.assertIsInstance(adf_text(node), str)

    def test_jira_rejects_a_malformed_issue_rather_than_importing_it(self):
        with patch("platform_core.connector_kinds.api_json", return_value={"issues": [{"k": 1}]}):
            with self.assertRaises(ValidationError):
                jira_records({"base_url": "https://team.atlassian.net", "auth": "datacenter"}, "t")

    def test_servicenow_maps_configured_fields(self):
        payload = {
            "result": [
                {
                    "sys_id": "abc123",
                    "number": "INC0001",
                    "short_description": "Node down",
                    "description": "NODE1 is unreachable.",
                }
            ]
        }
        with patch("platform_core.connector_kinds.api_json", return_value=payload):
            records = servicenow_records(
                {
                    "base_url": "https://acme.service-now.com",
                    "auth": "basic",
                    "identity": "svc",
                    "table": "incident",
                    "title_field": "short_description",
                    "body_field": "description",
                },
                "password",
            )
        self.assertEqual(records[0].title, "Node down")
        self.assertIn("NODE1 is unreachable.", records[0].body)
        self.assertIn("abc123", records[0].url)

    def test_servicenow_oauth_exchanges_the_secret_for_a_token(self):
        exchanged = {}

        def fake_fetch(url, **kwargs):
            exchanged["url"] = url
            exchanged["body"] = kwargs.get("body")
            exchanged["method"] = kwargs.get("method")
            return json.dumps({"access_token": "issued"}).encode(), "application/json", "", url

        config = {
            "base_url": "https://acme.service-now.com",
            "auth": "oauth",
            "identity": "client-id",
            "table": "incident",
            "title_field": "short_description",
            "body_field": "description",
        }
        with patch("platform_core.connector_kinds.fetch", side_effect=fake_fetch):
            with patch(
                "platform_core.connector_kinds.api_json", return_value={"result": []}
            ) as api:
                servicenow_records(config, "client-secret")
        self.assertEqual(exchanged["method"], "POST")
        self.assertIn("oauth_token.do", exchanged["url"])
        self.assertIn(b"grant_type=client_credentials", exchanged["body"])
        # The issued token, not the client secret, is what reaches the table API.
        self.assertEqual(api.call_args.args[1]["Authorization"], "Bearer issued")

    def test_servicenow_refuses_a_sign_in_that_returns_no_token(self):
        with patch(
            "platform_core.connector_kinds.fetch",
            return_value=(b"{}", "application/json", "", "https://x"),
        ):
            with self.assertRaises(ValidationError):
                servicenow_records(
                    {
                        "base_url": "https://acme.service-now.com",
                        "auth": "oauth",
                        "identity": "c",
                        "table": "incident",
                    },
                    "s",
                )


@override_settings(**SETTINGS)
class ConnectorManagementTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.url = reverse("connectors", args=[self.app.pk])

    def make(self, kind="jira", name="Ops board"):
        return Connector.objects.create(
            application=self.app,
            kind=kind,
            name=name,
            config={"base_url": "https://team.atlassian.net", "auth": "datacenter"},
        )

    def test_several_connectors_can_exist_for_one_application(self):
        """The model used to be one-to-one, which made this impossible."""
        self.make("jira", "Ops board")
        self.make("servicenow", "Incidents")
        self.make("github", "Repo")
        self.assertEqual(Connector.objects.filter(application=self.app).count(), 3)
        response = self.client.get(self.url)
        for name in ("Ops board", "Incidents", "Repo"):
            self.assertContains(response, name)

    def test_the_list_is_owner_only(self):
        self.client.force_login(self.viewer, backend="django.contrib.auth.backends.ModelBackend")
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_a_connector_can_be_disabled_and_enabled(self):
        connector = self.make()
        self.client.post(self.url, {"action": "disable", "connector": connector.pk})
        connector.refresh_from_db()
        self.assertFalse(connector.enabled)
        self.client.post(self.url, {"action": "enable", "connector": connector.pk})
        connector.refresh_from_db()
        self.assertTrue(connector.enabled)

    def test_removing_a_connector_keeps_the_knowledge_it_imported(self):
        connector = self.make()
        from platform_core.workbench import add_knowledge

        add_knowledge(self.owner, self.app.pk, "Imported", "Body", source="https://x/1")
        self.client.post(self.url, {"action": "delete", "connector": connector.pk})
        self.assertFalse(Connector.objects.filter(pk=connector.pk).exists())
        self.assertTrue(KnowledgeEntry.objects.filter(title="Imported").exists())

    def test_a_disabled_connector_refuses_to_import(self):
        connector = self.make()
        connector.enabled = False
        connector.save(update_fields=["enabled"])
        with self.assertRaises(ValidationError):
            sync(self.owner, connector.pk, self.app.pk)

    def test_a_kind_that_needs_a_credential_says_so_rather_than_calling_out(self):
        connector = self.make()
        with patch("platform_core.connectors.credential", return_value=""):
            with patch("platform_core.connector_kinds.api_json") as api:
                with self.assertRaises(ValidationError) as raised:
                    sync(self.owner, connector.pk, self.app.pk)
        api.assert_not_called()
        self.assertIn(f"jira_{self.app.pk}", " ".join(raised.exception.messages))

    def test_a_failure_is_recorded_on_the_row_for_the_list_to_show(self):
        connector = self.make()
        with patch("platform_core.connectors.credential", return_value="tok"):
            with patch(
                "platform_core.connector_kinds.api_json",
                side_effect=ValidationError("Jira: that host is not reachable."),
            ):
                with self.assertRaises(ValidationError):
                    sync(self.owner, connector.pk, self.app.pk)
        connector.refresh_from_db()
        self.assertEqual(connector.last_status, "failed")
        self.assertIn("not reachable", connector.last_error)

    def test_a_viewer_cannot_import(self):
        connector = self.make()
        with self.assertRaises(PermissionDenied):
            sync(self.viewer, connector.pk, self.app.pk)

    def test_a_successful_import_records_its_outcome(self):
        connector = self.make()
        payload = {
            "issues": [
                {
                    "key": "OPS-1",
                    "fields": {"summary": "Queue full", "description": "Beta is full."},
                }
            ]
        }
        with patch("platform_core.connectors.credential", return_value="tok"):
            with patch("platform_core.connector_kinds.api_json", return_value=payload):
                self.assertEqual(sync(self.owner, connector.pk, self.app.pk), 1)
        connector.refresh_from_db()
        self.assertEqual(connector.last_status, "ok")
        self.assertEqual(connector.last_count, 1)
        self.assertIsNotNone(connector.last_synced_at)

    def test_the_form_saves_a_connector(self):
        """Every existing test built rows through the ORM, so nothing exercised
        the form - and `full_clean` rejected every one of them, for every kind,
        because `last_synced_at` was null on a connector that had never run."""
        response = self.client.post(
            reverse("connector-new", args=[self.app.pk]) + "?kind=jira",
            {
                "name": "CARE board",
                "base_url": "https://team.atlassian.net",
                "auth": "cloud",
                "account_email": "ops@example.com",
                "jql": "project = CARE ORDER BY updated DESC",
            },
        )
        self.assertRedirects(response, self.url)
        saved = Connector.objects.get(application=self.app, name="CARE board")
        self.assertEqual(saved.kind, "jira")
        self.assertEqual(saved.config["base_url"], "https://team.atlassian.net")
        self.assertEqual(saved.config["account_email"], "ops@example.com")
        self.assertIsNone(saved.last_synced_at)

    def test_the_form_edits_a_connector_in_place(self):
        connector = self.make()
        response = self.client.post(
            reverse("connector-edit", args=[self.app.pk, connector.pk]),
            {
                "name": "Ops board",
                "base_url": "https://team.atlassian.net",
                "auth": "datacenter",
                "jql": "project = OPS",
            },
        )
        self.assertRedirects(response, self.url)
        connector.refresh_from_db()
        self.assertEqual(connector.config["jql"], "project = OPS")
        self.assertEqual(Connector.objects.filter(application=self.app).count(), 1)

    def test_a_duplicate_name_is_reported_rather_than_raised(self):
        """The model's uniqueness check is the one path that reaches the error
        branch, which read `messages` as a dictionary and raised instead."""
        self.make("jira", "Ops board")
        response = self.client.post(
            reverse("connector-new", args=[self.app.pk]) + "?kind=jira",
            {
                "name": "Ops board",
                "base_url": "https://team.atlassian.net",
                "auth": "datacenter",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Connector.objects.filter(application=self.app).count(), 1)
        self.assertContains(response, "already exists")

    def test_a_status_change_is_imported_as_a_change(self):
        """The defect this closes: status was never fetched, so the stored text
        was identical after a transition and sync imported nothing. A board could
        be synced all day and never show that a ticket had been fixed."""
        connector = self.make()

        def board(status):
            return {
                "issues": [
                    {
                        "key": "OPS-1",
                        "fields": {
                            "summary": "Queue full",
                            "description": "Beta is full.",
                            "status": {"name": status},
                            "issuetype": {"name": "Bug"},
                        },
                    }
                ]
            }

        with patch("platform_core.connectors.credential", return_value="tok"):
            with patch("platform_core.connector_kinds.api_json", return_value=board("To Do")):
                self.assertEqual(sync(self.owner, connector.pk, self.app.pk), 1)
            with patch("platform_core.connector_kinds.api_json", return_value=board("To Do")):
                self.assertEqual(sync(self.owner, connector.pk, self.app.pk), 0)
            with patch("platform_core.connector_kinds.api_json", return_value=board("Done")):
                self.assertEqual(sync(self.owner, connector.pk, self.app.pk), 1)
        current = KnowledgeEntry.objects.get(
            application=self.app, source__endswith="/browse/OPS-1", active=True
        )
        self.assertIn("Status: Done", current.content)
        self.assertIn("Type: Bug", current.content)
        self.assertIn("Beta is full.", current.content)

    def test_a_vanished_record_is_retired_only_when_pruning_is_asked_for(self):
        connector = self.make()
        both = {
            "issues": [
                {"key": "OPS-1", "fields": {"summary": "One", "description": "a"}},
                {"key": "OPS-2", "fields": {"summary": "Two", "description": "b"}},
            ]
        }
        one = {"issues": [{"key": "OPS-1", "fields": {"summary": "One", "description": "a"}}]}
        with patch("platform_core.connectors.credential", return_value="tok"):
            with patch("platform_core.connector_kinds.api_json", return_value=both):
                sync(self.owner, connector.pk, self.app.pk)
            self.assertEqual(self.active_count(), 2)
            # Off by default: absence is not deletion under a narrowing filter.
            with patch("platform_core.connector_kinds.api_json", return_value=one):
                sync(self.owner, connector.pk, self.app.pk)
            self.assertEqual(self.active_count(), 2)
            Connector.objects.filter(pk=connector.pk).update(prune_missing=True)
            with patch("platform_core.connector_kinds.api_json", return_value=one):
                sync(self.owner, connector.pk, self.app.pk)
        self.assertEqual(self.active_count(), 1)
        # Retired, not deleted: the row stays for plan hashes and audit evidence.
        self.assertTrue(
            KnowledgeEntry.objects.filter(source__endswith="/browse/OPS-2", active=False).exists()
        )

    def test_pruning_stands_down_when_a_second_connector_shares_the_site(self):
        """Two connectors onto one Jira with different filters would otherwise
        retire each other's imports, each correctly finding the records absent
        from its own results."""
        first = self.make("jira", "Ops board")
        Connector.objects.filter(pk=first.pk).update(prune_missing=True)
        self.make("jira", "Security board")
        with patch("platform_core.connectors.credential", return_value="tok"):
            with patch(
                "platform_core.connector_kinds.api_json",
                return_value={"issues": [{"key": "OPS-1", "fields": {"summary": "One"}}]},
            ):
                sync(self.owner, first.pk, self.app.pk)
            add_knowledge(
                self.owner, self.app.pk, "Other", "Body",
                source="https://team.atlassian.net/browse/SEC-9",
            )
            with patch(
                "platform_core.connector_kinds.api_json",
                return_value={"issues": [{"key": "OPS-1", "fields": {"summary": "One"}}]},
            ):
                sync(self.owner, Connector.objects.get(pk=first.pk).pk, self.app.pk)
        self.assertTrue(
            KnowledgeEntry.objects.filter(source__endswith="/browse/SEC-9", active=True).exists()
        )

    def test_a_full_page_is_never_treated_as_a_complete_answer(self):
        """At the cap the result is a page, not the whole set, so everything
        past it would look deleted."""
        from platform_core.connector_kinds import KINDS, MAX_RECORDS, Record

        connector = self.make()
        Connector.objects.filter(pk=connector.pk).update(prune_missing=True)
        connector.refresh_from_db()
        add_knowledge(
            self.owner, self.app.pk, "Old", "Body",
            source="https://team.atlassian.net/browse/OPS-999",
        )
        page = [
            Record(str(n), f"Issue {n}", "b", f"https://team.atlassian.net/browse/OPS-{n}")
            for n in range(MAX_RECORDS)
        ]
        from platform_core.connectors import prune

        self.assertEqual(prune(connector, self.app, KINDS["jira"], page), 0)
        self.assertTrue(
            KnowledgeEntry.objects.filter(source__endswith="/browse/OPS-999", active=True).exists()
        )

    def active_count(self):
        return KnowledgeEntry.objects.filter(
            application=self.app, active=True, source__contains="/browse/"
        ).count()

    def test_only_a_connector_whose_interval_has_elapsed_is_due(self):
        from datetime import timedelta

        from django.utils import timezone

        from platform_core.connectors import due_connector

        connector = self.make()
        self.assertIsNone(due_connector(), "manual-only connectors are never due")
        Connector.objects.filter(pk=connector.pk).update(sync_interval_minutes=60)
        self.assertIsNotNone(due_connector(), "never attempted, so due immediately")
        Connector.objects.filter(pk=connector.pk).update(last_attempt_at=timezone.now())
        self.assertIsNone(due_connector())
        Connector.objects.filter(pk=connector.pk).update(
            last_attempt_at=timezone.now() - timedelta(minutes=61)
        )
        self.assertIsNotNone(due_connector())
        Connector.objects.filter(pk=connector.pk).update(enabled=False)
        self.assertIsNone(due_connector(), "a disabled connector never runs by itself")

    def test_a_failed_attempt_waits_its_interval_rather_than_spinning(self):
        """last_attempt_at is written on both paths. Measuring the schedule from
        last_synced_at instead would retry a broken instance on every tick."""
        from platform_core.connectors import due_connector, process_next_connector

        connector = self.make()
        Connector.objects.filter(pk=connector.pk).update(
            sync_interval_minutes=60, created_by=self.owner
        )
        with patch("platform_core.connectors.credential", return_value="tok"):
            with patch(
                "platform_core.connector_kinds.api_json",
                side_effect=ValidationError("Jira: unreachable"),
            ):
                self.assertTrue(process_next_connector())
        connector.refresh_from_db()
        self.assertEqual(connector.last_status, "failed")
        self.assertIsNotNone(connector.last_attempt_at)
        self.assertIsNone(connector.last_synced_at)
        self.assertIsNone(due_connector())

    def test_a_scheduled_run_is_the_creator_run_and_stops_with_their_grant(self):
        """The property that makes unattended imports safe: no privileged
        identity exists for them. Revoking the grant stops the schedule, with no
        code that knows about schedules."""
        from platform_core.connectors import process_next_connector
        from platform_core.models import ApplicationGrant

        connector = self.make()
        Connector.objects.filter(pk=connector.pk).update(
            sync_interval_minutes=60, created_by=self.owner
        )
        payload = {"issues": [{"key": "OPS-1", "fields": {"summary": "One", "description": "a"}}]}
        with patch("platform_core.connectors.credential", return_value="tok"):
            with patch("platform_core.connector_kinds.api_json", return_value=payload):
                self.assertTrue(process_next_connector())
        connector.refresh_from_db()
        self.assertEqual(connector.last_status, "ok")
        self.assertTrue(
            KnowledgeEntry.objects.filter(source__endswith="/browse/OPS-1", active=True).exists()
        )
        # The run is attributed to the creator, not to a background identity.
        event = AuditEvent.objects.filter(action="connector.synced").latest("created_at")
        self.assertEqual(event.actor, self.owner)

        ApplicationGrant.objects.filter(application=self.app, user=self.owner).update(role="viewer")
        Connector.objects.filter(pk=connector.pk).update(last_attempt_at=None)
        with patch("platform_core.connectors.credential", return_value="tok"):
            with patch("platform_core.connector_kinds.api_json", return_value=payload):
                self.assertTrue(process_next_connector())
        connector.refresh_from_db()
        self.assertEqual(connector.last_status, "failed")
        self.assertIn("no longer owns", connector.last_error)

    def test_a_connector_whose_creator_is_gone_says_so_rather_than_escalating(self):
        from platform_core.connectors import process_next_connector

        connector = self.make()
        Connector.objects.filter(pk=connector.pk).update(
            sync_interval_minutes=60, created_by=None
        )
        self.assertTrue(process_next_connector())
        connector.refresh_from_db()
        self.assertEqual(connector.last_status, "failed")
        self.assertIn("created this connector", connector.last_error)

    def test_the_form_saves_a_schedule(self):
        response = self.client.post(
            reverse("connector-new", args=[self.app.pk]) + "?kind=jira",
            {
                "name": "Scheduled board",
                "base_url": "https://team.atlassian.net",
                "auth": "datacenter",
                "interval": "60",
                "prune": "on",
            },
        )
        self.assertRedirects(response, self.url)
        saved = Connector.objects.get(name="Scheduled board")
        self.assertEqual(saved.sync_interval_minutes, 60)
        self.assertTrue(saved.prune_missing)

    def test_an_interval_that_is_not_offered_is_read_as_manual(self):
        """The choices are a closed list; a posted value outside it must not
        become a schedule nobody selected."""
        self.client.post(
            reverse("connector-new", args=[self.app.pk]) + "?kind=jira",
            {
                "name": "Odd board",
                "base_url": "https://team.atlassian.net",
                "auth": "datacenter",
                "interval": "1",
            },
        )
        self.assertEqual(Connector.objects.get(name="Odd board").sync_interval_minutes, 0)

    def test_the_analysis_queue_defaults_to_a_configured_connector(self):
        """"Any connector" is not a choice when one is configured - it is the
        same choice, written twice, with the wrong one preselected."""
        from platform_core.workbench import add_knowledge

        connector = self.make("jira", "Ops board")
        add_knowledge(
            self.owner, self.app.pk, "OPS-1", "A ticket",
            source="https://team.atlassian.net/browse/OPS-1",
        )
        page = self.client.get(reverse("plans", args=[self.app.pk]))
        self.assertContains(page, f'value="{connector.pk}" selected')
        self.assertNotContains(page, "Any connector")

    def test_a_second_connector_brings_back_the_any_option(self):
        from platform_core.workbench import add_knowledge

        self.make("jira", "Ops board")
        self.make("github", "Repo")
        # The section only renders when there is something to analyse.
        add_knowledge(
            self.owner, self.app.pk, "OPS-1", "A ticket",
            source="https://team.atlassian.net/browse/OPS-1",
        )
        page = self.client.get(reverse("plans", args=[self.app.pk]))
        self.assertContains(page, "Any connector")

    def test_one_definition_of_a_connector_url_prefix(self):
        """`code_factory.connector_prefix` and `Kind.namespace` had drifted: the
        first returned a Jira site's base URL where records carry /browse/."""
        from platform_core.code_factory import connector_prefix

        connector = self.make("jira", "Ops board")
        self.assertEqual(
            connector_prefix(connector), KINDS["jira"].namespace(connector.config)
        )
        self.assertTrue(connector_prefix(connector).endswith("/browse/"))

    def test_every_registered_kind_declares_a_usable_icon(self):
        from platform_core.templatetags.icons import ICONS

        for kind in KINDS.values():
            self.assertIn(kind.icon, ICONS, f"{kind.key} names an icon that does not exist")

    def test_each_kind_carries_its_own_mark_rather_than_a_shared_one(self):
        """A generic icon on every row makes the list harder to scan, not easier."""
        marks = [kind.icon for kind in KINDS.values()]
        self.assertEqual(len(marks), len(set(marks)))

    def test_systems_are_named_as_themselves(self):
        """The list already has a column for what was imported; the name is the system."""
        self.assertEqual([k.label for k in KINDS.values()], ["GitHub", "Jira", "ServiceNow"])
        for kind in KINDS.values():
            self.assertNotIn(" ", kind.label)
            self.assertTrue(kind.summary, f"{kind.key} has no summary for its card")

    def test_the_model_and_the_registry_agree_on_names(self):
        from platform_core.models import CONNECTOR_KINDS

        self.assertEqual(
            dict(CONNECTOR_KINDS), {key: kind.label for key, kind in KINDS.items()}
        )

    def test_a_filled_mark_is_not_rendered_as_an_outline(self):
        from django.template import Context, Template

        from platform_core.templatetags.icons import FILLED

        markup = Template('{% load icons %}{% icon "github" %}').render(Context())
        self.assertIn("github", FILLED)
        self.assertIn('fill="currentColor"', markup)
        self.assertIn('stroke="none"', markup)
        stroked = Template('{% load icons %}{% icon "jira" %}').render(Context())
        self.assertIn('fill="none"', stroked)
