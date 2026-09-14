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
from platform_core.models import Connector, KnowledgeEntry

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

    def test_every_registered_kind_declares_a_usable_icon(self):
        from platform_core.templatetags.icons import ICONS

        for kind in KINDS.values():
            self.assertIn(kind.icon, ICONS, f"{kind.key} names an icon that does not exist")
