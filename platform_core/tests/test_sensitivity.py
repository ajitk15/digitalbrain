"""A restricted source is never read or cited by AI; personal data is only suggested."""

from io import StringIO
from types import SimpleNamespace

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from platform_core.agent_runtime.tools import NOT_FOUND, fetch_source, verify_citations
from platform_core.graph_ai import graph_citations
from platform_core.graphs import build_graph, sources_for
from platform_core.models import (
    ApplicationGrant,
    AuditEvent,
    GraphRevision,
    KnowledgeEntry,
    OrganizationMember,
    User,
)
from platform_core.sensitivity import scan, summary
from platform_core.workbench import add_knowledge, retrieve

from . import test_documents


class ScanTests(SimpleTestCase):
    def test_each_kind_is_found(self):
        found = scan(
            "Mail jane.doe@example.com or call +44 20 7946 0958. SSN 123-45-6789. "
            "Card 4111 1111 1111 1111. Date of birth: 12/04/1961."
        )
        self.assertEqual(
            found,
            {
                "email address": 1,
                "telephone number": 1,
                "social security number": 1,
                "payment card number": 1,
                "date of birth": 1,
            },
        )

    def test_versions_dates_and_non_luhn_digits_are_not_personal_data(self):
        self.assertEqual(
            scan("Release 1.0.2 shipped 2026-08-14. Order 1234 5678 9012 3456. Port 8080."), {}
        )

    def test_summary_reads_as_words(self):
        self.assertEqual(
            summary({"email address": 2, "date of birth": 1}),
            "1 date of birth, 2 email addresses",
        )


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class RestrictionTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.contributor = User.objects.create_user("contributor")
        OrganizationMember.objects.create(
            organization=self.app.product.portfolio.organization, user=self.contributor
        )
        ApplicationGrant.objects.create(
            application=self.app, user=self.contributor, role="contributor"
        )
        self.secret = add_knowledge(
            self.owner,
            self.app.pk,
            "Patient roster",
            "Consent export roster.\nJane Doe consents to export, jane.doe@example.com.",
            source="https://jira.example/browse/CARE-1",
        )
        self.public = add_knowledge(
            self.owner, self.app.pk, "Export design", "Consent export is checked first."
        )

    def restrict(self, entry=None, user=None, action="restrict"):
        self.client.force_login(
            user or self.owner, backend="django.contrib.auth.backends.ModelBackend"
        )
        return self.client.post(
            reverse("knowledge-detail", args=[self.app.pk, (entry or self.secret).pk]),
            {"action": action},
        )

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

    def test_personal_data_is_recorded_when_added_and_shown(self):
        self.assertEqual(self.secret.personal_data, {"email address": 1})
        self.assertEqual(self.secret.sensitivity, "standard")
        response = self.client.get(reverse("knowledge-detail", args=[self.app.pk, self.secret.pk]))
        self.assertContains(response, "1 email address")

    def test_a_restricted_source_is_out_of_search_graph_and_tools(self):
        self.publish()
        cited = {c["id"] for c in graph_citations(self.app.pk, "consent export roster")}
        self.assertIn(str(self.secret.pk), cited)
        self.assertEqual(self.restrict().status_code, 302)
        # Already-published versions stop citing it at once: no rebuild needed.
        cited = {c["id"] for c in graph_citations(self.app.pk, "consent export roster")}
        self.assertNotIn(str(self.secret.pk), cited)
        self.assertIn(str(self.public.pk), cited)
        self.assertNotIn(self.secret.pk, [e.pk for _, e, _ in retrieve(self.app, "roster")])
        self.assertNotIn(self.secret.pk, [e.pk for e in sources_for(self.app.pk)])
        scope = SimpleNamespace(user_id=self.owner.pk, app_id=self.app.pk)
        self.assertEqual(fetch_source(scope, {"source_id": str(self.secret.pk)}).text, NOT_FOUND)
        self.assertEqual(
            verify_citations(
                self.app.pk,
                [{"id": str(self.secret.pk), "quote": "Jane Doe consents to export"}],
            ),
            [],
        )
        self.assertTrue(AuditEvent.objects.filter(action="knowledge.restricted").exists())

    def test_people_can_still_read_it(self):
        self.restrict()
        response = self.client.get(reverse("knowledge-detail", args=[self.app.pk, self.secret.pk]))
        self.assertContains(response, "Restricted - never used by AI")
        self.assertContains(response, "Jane Doe")

    def test_a_contributor_restricts_but_only_an_owner_lifts_it(self):
        self.assertEqual(self.restrict(user=self.contributor).status_code, 302)
        self.assertEqual(self.restrict(user=self.contributor, action="unrestrict").status_code, 403)
        self.assertEqual(self.restrict(user=self.viewer).status_code, 403)
        self.assertEqual(self.restrict(action="unrestrict").status_code, 302)
        self.secret.refresh_from_db()
        self.assertEqual(self.secret.sensitivity, "standard")

    def test_the_next_import_of_the_same_record_stays_restricted(self):
        self.restrict()
        again = add_knowledge(
            self.owner,
            self.app.pk,
            "Patient roster",
            "Updated roster.",
            source="https://jira.example/browse/CARE-1",
        )
        self.assertEqual(again.sensitivity, "restricted")

    def test_code_factory_will_not_analyse_a_restricted_ticket(self):
        from platform_core.code_factory import start_run, ticket_choices

        self.restrict()
        self.assertNotIn(self.secret.pk, [e.pk for e in ticket_choices(self.app)])
        self.secret.refresh_from_db()
        with self.assertRaises(ValidationError):
            start_run(self.owner, self.app.pk, self.secret)

    def test_an_unknown_action_is_refused_and_archive_still_works(self):
        url = reverse("knowledge-detail", args=[self.app.pk, self.public.pk])
        self.assertEqual(self.client.post(url, {"action": "delete"}).status_code, 400)
        self.assertEqual(self.client.post(url).status_code, 302)
        self.assertFalse(KnowledgeEntry.objects.get(pk=self.public.pk).active)

    def test_the_backfill_command_only_records(self):
        KnowledgeEntry.objects.update(personal_data={})
        out = StringIO()
        call_command("scan_personal_data", stdout=out)
        self.assertIn("1 look like", out.getvalue())
        self.secret.refresh_from_db()
        self.assertEqual(self.secret.personal_data, {"email address": 1})
        self.assertEqual(self.secret.sensitivity, "standard")
