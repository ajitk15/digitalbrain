"""Answers cite a record only to the members its source system lets open it."""

from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from platform_core import permissions
from platform_core.agent_runtime.tools import NOT_FOUND, fetch_source, verify_citations
from platform_core.benchmark import run as run_benchmark
from platform_core.code_factory import start_run, ticket_choices
from platform_core.connectors import sync
from platform_core.graph_ai import graph_citations
from platform_core.graphs import build_graph, sources_for
from platform_core.models import (
    ApplicationGrant,
    BenchmarkQuestion,
    Connector,
    ExternalIdentity,
    GraphRevision,
    KnowledgeEntry,
)
from platform_core.workbench import retrieve

from . import test_documents

SITE = "https://team.atlassian.net"


class FakeJira:
    """Answers the three Jira calls the platform makes, from a little table."""

    def __init__(self, issues, emails, browse):
        self.issues = issues  # key -> summary
        self.emails = emails  # email -> accountId (or None: address hidden)
        self.browse = browse  # key -> set of accountIds
        self.calls = []

    def __call__(self, url, headers, *, label):
        self.calls.append(url)
        parts = urlsplit(url)
        query = {key: values[0] for key, values in parse_qs(parts.query).items()}
        if parts.path.endswith("/search/jql"):
            return {
                "issues": [
                    {"key": key, "fields": {"summary": summary}}
                    for key, summary in self.issues.items()
                ]
            }
        if parts.path.endswith("/user/search"):
            email = query["query"]
            account = self.emails.get(email)
            if account is None:
                return [{"accountId": "hidden"}] if email in self.emails else []
            return [{"accountId": account, "emailAddress": email}]
        if parts.path.endswith("/user/viewissue/search"):
            account = query["accountId"]
            return (
                [{"accountId": account}]
                if account in self.browse.get(query["issueKey"], set())
                else []
            )
        raise AssertionError(url)


class PermittedTests(SimpleTestCase):
    def test_who_may_see_what(self):
        anyone = SimpleNamespace(access_principals=None)
        listed = SimpleNamespace(access_principals=["7"])
        closed = SimpleNamespace(access_principals=[])
        seven, eight = SimpleNamespace(pk=7), SimpleNamespace(pk=8)
        self.assertTrue(permissions.permitted(anyone, None))
        self.assertTrue(permissions.permitted(listed, seven))
        self.assertFalse(permissions.permitted(listed, eight))
        self.assertFalse(permissions.permitted(listed, None))
        self.assertFalse(permissions.permitted(closed, seven))
        self.assertTrue(permissions.permitted(closed, permissions.EVERYONE))

    def test_the_issue_key_comes_from_the_browse_address(self):
        self.assertEqual(permissions.issue_key(f"{SITE}/browse/KAN-4"), "KAN-4")
        self.assertEqual(permissions.issue_key("https://x/other/KAN-4"), "")


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class EnforcementTests(TestCase):
    """Two members; the viewer may not open KAN-1 in Jira."""

    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.owner.email, self.viewer.email = "owner@example.com", "viewer@example.com"
        self.owner.save()
        self.viewer.save()
        self.connector = Connector.objects.create(
            application=self.app,
            kind="jira",
            name="Board",
            config={"base_url": SITE, "auth": "cloud", "account_email": "bot@example.com"},
            enforce_permissions=True,
        )
        self.jira = FakeJira(
            issues={"KAN-1": "Consent export audit", "KAN-2": "Consent export design"},
            emails={"owner@example.com": "acc-owner", "viewer@example.com": "acc-viewer"},
            browse={"KAN-1": {"acc-owner"}, "KAN-2": {"acc-owner", "acc-viewer"}},
        )
        self.import_board()
        self.secret = KnowledgeEntry.objects.get(source=f"{SITE}/browse/KAN-1", active=True)
        self.open = KnowledgeEntry.objects.get(source=f"{SITE}/browse/KAN-2", active=True)

    def import_board(self):
        with patch("platform_core.connectors.credential", return_value="token"):
            with patch("platform_core.connector_kinds.api_json", side_effect=self.jira):
                return sync(self.owner, self.connector.pk, self.app.pk)

    def publish(self):
        entries = list(sources_for(self.app.pk))
        data, quality = build_graph(entries, len(entries))
        GraphRevision.objects.create(
            application=self.app,
            number=1,
            fingerprint="x",
            data=data,
            quality=quality,
            published_at=timezone.now(),
        )

    def test_an_import_records_who_may_open_each_issue(self):
        self.assertEqual(self.secret.access_principals, [str(self.owner.pk)])
        self.assertEqual(
            sorted(self.open.access_principals), sorted([str(self.owner.pk), str(self.viewer.pk)])
        )
        self.assertIsNotNone(self.secret.access_checked_at)
        self.assertEqual(ExternalIdentity.objects.get(user=self.viewer).account, "acc-viewer")

    def test_graph_answers_follow_the_asker(self):
        self.publish()
        question = "consent export audit"

        def cited(user):
            return {c["id"] for c in graph_citations(self.app.pk, question, user=user)}

        self.assertIn(str(self.secret.pk), cited(self.owner))
        self.assertNotIn(str(self.secret.pk), cited(self.viewer))
        self.assertIn(str(self.open.pk), cited(self.viewer))
        # Nobody asking: only what no system restricts.
        self.assertNotIn(str(self.secret.pk), cited(None))

    def test_keyword_search_and_tools_follow_the_asker(self):
        def found(user):
            return [e.pk for _, e, _ in retrieve(self.app, "consent export", user)]

        self.assertIn(self.secret.pk, found(self.owner))
        self.assertNotIn(self.secret.pk, found(self.viewer))
        scope = SimpleNamespace(user_id=self.viewer.pk, app_id=self.app.pk)
        self.assertEqual(fetch_source(scope, {"source_id": str(self.secret.pk)}).text, NOT_FOUND)
        citation = {
            "id": str(self.secret.pk),
            "digest": self.secret.digest,
            "excerpt": self.secret.content[:20],
        }
        self.assertEqual(verify_citations(self.app.pk, [citation], self.viewer), [])
        self.assertEqual(len(verify_citations(self.app.pk, [citation], self.owner)), 1)

    def test_code_factory_will_not_take_a_ticket_its_requester_cannot_open(self):
        self.assertNotIn(
            self.secret.pk, [e.pk for e in ticket_choices(self.app, viewer=self.viewer)]
        )
        self.assertIn(self.secret.pk, [e.pk for e in ticket_choices(self.app, viewer=self.owner)])
        # A contributor, so the refusal is the source system's, not the role's.
        ApplicationGrant.objects.filter(application=self.app, user=self.viewer).update(
            role="contributor"
        )
        with self.assertRaises(ValidationError) as refused:
            start_run(self.viewer, self.app.pk, self.secret)
        self.assertIn("not available", " ".join(refused.exception.messages))

    def test_the_benchmark_measures_the_whole_corpus(self):
        self.publish()
        BenchmarkQuestion.objects.create(
            application=self.app, question="consent export audit", expected="audit"
        )
        self.assertEqual(run_benchmark(self.app, GraphRevision.objects.get()).found, 1)

    def test_a_member_with_no_account_is_left_out_and_the_connector_says_so(self):
        KnowledgeEntry.objects.update(access_checked_at=None)
        ExternalIdentity.objects.all().delete()
        self.jira.emails["viewer@example.com"] = None  # address hidden by Jira
        self.import_board()
        self.open.refresh_from_db()
        self.assertEqual(self.open.access_principals, [str(self.owner.pk)])
        self.connector.refresh_from_db()
        self.assertIn("viewer", self.connector.last_error)

    def test_an_operator_can_link_an_account_by_hand(self):
        KnowledgeEntry.objects.update(access_checked_at=None)
        ExternalIdentity.objects.all().delete()
        self.jira.emails["viewer@example.com"] = None
        call_command(
            "link_identity", "viewer", "--site", SITE, "--account", "acc-viewer", stdout=StringIO()
        )
        self.import_board()
        self.open.refresh_from_db()
        self.assertIn(str(self.viewer.pk), self.open.access_principals)

    def test_a_changed_issue_keeps_its_list_until_checked_again(self):
        self.jira.issues["KAN-1"] = "Consent export audit, revised"
        self.jira.browse["KAN-1"] = {"acc-owner", "acc-viewer"}
        self.import_board()
        revised = KnowledgeEntry.objects.get(source=f"{SITE}/browse/KAN-1", active=True)
        self.assertNotEqual(revised.pk, self.secret.pk)
        self.assertIn(str(self.viewer.pk), revised.access_principals)

    def test_a_failed_check_leaves_new_issues_closed(self):
        self.jira.issues["KAN-3"] = "Consent export runbook"

        def failing(url, headers, *, label):
            if "/user/" in url:
                raise ValidationError("Jira: that host is not reachable.")
            return self.jira(url, headers, label=label)

        ExternalIdentity.objects.all().delete()
        with patch("platform_core.connectors.credential", return_value="token"):
            with patch("platform_core.connector_kinds.api_json", side_effect=failing):
                sync(self.owner, self.connector.pk, self.app.pk)
        new = KnowledgeEntry.objects.get(source=f"{SITE}/browse/KAN-3", active=True)
        self.assertEqual(new.access_principals, [])
        self.connector.refresh_from_db()
        self.assertIn("Permission check failed", self.connector.last_error)

    def test_the_check_stops_at_its_cap(self):
        KnowledgeEntry.objects.update(access_checked_at=None, access_principals=[])
        with patch.object(permissions, "MAX_CHECKS", 2):
            self.import_board()
        checked = KnowledgeEntry.objects.filter(active=True, access_checked_at__isnull=False)
        self.assertEqual(checked.count(), 1)

    def test_switching_enforcement_off_and_on(self):
        self.client.post(
            reverse("connector-edit", args=[self.app.pk, self.connector.pk]),
            {
                "name": "Board",
                "base_url": SITE,
                "auth": "cloud",
                "account_email": "bot@example.com",
            },
        )
        self.secret.refresh_from_db()
        self.assertIsNone(self.secret.access_principals)
        self.client.post(
            reverse("connector-edit", args=[self.app.pk, self.connector.pk]),
            {
                "name": "Board",
                "base_url": SITE,
                "auth": "cloud",
                "account_email": "bot@example.com",
                "enforce": "on",
            },
        )
        self.secret.refresh_from_db()
        # Closed at once, not visible until the next import notices.
        self.assertEqual(self.secret.access_principals, [])

    def test_the_option_says_it_governs_answers_not_reading(self):
        """Any member can still open a source's page and read it in full; the
        form must not imply the option hides more than answers."""
        form = self.client.get(reverse("connector-edit", args=[self.app.pk, self.connector.pk]))
        self.assertContains(form, "connector-enforce")
        self.assertContains(form, "This governs answers, not reading:")
        self.assertContains(form, "read it in full")

    def test_unsupported_kinds_cannot_enforce(self):
        self.assertNotIn("servicenow", permissions.SUPPORTED)
        add = self.client.get(reverse("connector-new", args=[self.app.pk]) + "?kind=servicenow")
        self.assertNotContains(add, "connector-enforce")
