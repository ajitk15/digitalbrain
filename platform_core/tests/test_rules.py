"""Documents an owner says the work must be checked against (`rules.py`).

These pin what keeps a rule honest: statements are read by rules and each is
quoted from one line, a rule whose line changed or whose source was restricted
does not show, only an owner decides what is rules and that is audited, the
ranking shows what clearly applies rather than a list, and a rule only adds a
check to a plan review - it never decides one.
"""

import hashlib

from django.db import transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from platform_core import rules
from platform_core.models import (
    ApplicationGrant,
    AuditEvent,
    ChangePlan,
    Document,
    FactoryRun,
    KnowledgeEntry,
    OrganizationMember,
    PlanItem,
    User,
)
from platform_core.processing import supersede
from platform_core.workbench import add_knowledge

from . import test_documents

STANDARDS = """# Coding standards

Every service in this repository follows these rules.

## 1. Collections are bounded at the server

A list endpoint must cap its page size on the server; a client's limit is a
request, never a bound.

## 2. Nothing with a name goes in a log

Log the record's id. A patient's name, email or phone number never appears in a
log line, an exception message or a metric label.

## 3. The audit write shares the transaction

The audit row is written inside the transaction that makes the change, so a
rolled-back change leaves no audit row and a committed one always has its row.

## Controls

| ID | Control | Owner |
| --- | --- | --- |
| C-09 | Consent is checked before any export | Privacy |

Releases shall be tagged from main.
"""


@override_settings(
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
)
class RulesTests(TestCase):
    def setUp(self):
        # Called, not inherited or imported by name, so its tests run once.
        test_documents.DocumentTests.setUp(self)
        self.contributor = User.objects.create_user("contributor")
        OrganizationMember.objects.create(
            organization=self.app.product.portfolio.organization, user=self.contributor
        )
        ApplicationGrant.objects.create(
            application=self.app, user=self.contributor, role="contributor"
        )
        self.standards = add_knowledge(
            self.owner, self.app.pk, "Coding standards", STANDARDS
        )

    def mark(self, entry=None, user=None):
        entry = entry or self.standards
        KnowledgeEntry.objects.filter(pk=entry.pk).update(
            rules_by=user or self.owner, rules_at=timezone.now()
        )

    # Reading -------------------------------------------------------------

    def test_statements_are_read_by_rules_and_quoted_from_one_line(self):
        found = rules.statements(self.standards)
        self.assertEqual(
            [rule.statement for rule in found],
            [
                "Collections are bounded at the server",
                "Nothing with a name goes in a log",
                "The audit write shares the transaction",
                "C-09 · Consent is checked before any export · Privacy",
                "Releases shall be tagged from main.",
            ],
        )
        lines = STANDARDS.splitlines()
        for rule in found:
            self.assertEqual(lines[rule.line - 1], rule.quote)

    def test_a_must_inside_a_numbered_rule_is_that_rule_not_another(self):
        statements = [rule.statement for rule in rules.statements(self.standards)]
        self.assertFalse(any(text.startswith("A list endpoint must") for text in statements))
        self.assertFalse(any("never appears" in text for text in statements))

    # Applying ------------------------------------------------------------

    def test_nothing_applies_until_an_owner_marks_a_source(self):
        text = "Patient search logs the patient's name and email on every request."
        self.assertEqual(rules.applicable(self.app, text), [])
        self.mark()
        found = rules.applicable(self.app, text)
        self.assertEqual(found[0][0].statement, "Nothing with a name goes in a log")

    def test_only_rules_clearly_ahead_are_shown(self):
        self.mark()
        found = rules.applicable(
            self.app, "The audit row is written after the transaction commits, outside it."
        )
        self.assertEqual(found[0][0].statement, "The audit write shares the transaction")
        self.assertLess(len(found), rules.SHOWN)

    def test_work_sharing_too_little_with_any_rule_is_shown_none(self):
        self.mark()
        self.assertEqual(rules.applicable(self.app, "Button colour is wrong in dark mode."), [])

    def test_a_rule_whose_line_changed_does_not_show(self):
        self.mark()
        [rule] = [
            r for r in rules.statements(self.standards) if r.statement.startswith("Nothing")
        ]
        changed = STANDARDS.replace("Nothing with a name", "Nothing with a NAME")
        stale = KnowledgeEntry(pk=self.standards.pk, content=changed)
        self.assertFalse(rules.verified(rule, stale))
        self.assertTrue(rules.verified(rule, self.standards))

    def test_a_restricted_or_retired_source_is_no_rule(self):
        self.mark()
        text = "Patient search logs the patient's name and email on every request."
        KnowledgeEntry.objects.filter(pk=self.standards.pk).update(sensitivity="restricted")
        self.assertEqual(rules.applicable(self.app, text), [])
        KnowledgeEntry.objects.filter(pk=self.standards.pk).update(
            sensitivity="standard", active=False
        )
        self.assertEqual(rules.applicable(self.app, text), [])

    def test_another_applications_rules_never_apply(self):
        self.mark()
        self.assertEqual(
            rules.applicable(self.other, "Patient search logs the patient's name and email."),
            [],
        )

    # Marking -------------------------------------------------------------

    def test_an_owner_marks_a_source_as_rules_and_it_is_audited(self):
        url = reverse("knowledge-rules", args=[self.app.pk, self.standards.pk])
        response = self.client.post(url, {"rules": "on"})
        self.assertRedirects(
            response,
            reverse("knowledge-detail", args=[self.app.pk, self.standards.pk]) + "#rules",
            fetch_redirect_response=False,
        )
        self.standards.refresh_from_db()
        self.assertEqual(self.standards.rules_by, self.owner)
        self.assertTrue(AuditEvent.objects.filter(action="knowledge.rules_on").exists())
        self.client.post(url)
        self.standards.refresh_from_db()
        self.assertIsNone(self.standards.rules_by)
        self.assertIsNone(self.standards.rules_at)
        self.assertTrue(AuditEvent.objects.filter(action="knowledge.rules_off").exists())

    def test_only_an_owner_decides_what_the_work_is_checked_against(self):
        url = reverse("knowledge-rules", args=[self.app.pk, self.standards.pk])
        self.client.force_login(
            self.contributor, backend="django.contrib.auth.backends.ModelBackend"
        )
        self.assertEqual(self.client.post(url, {"rules": "on"}).status_code, 403)
        self.client.force_login(self.viewer, backend="django.contrib.auth.backends.ModelBackend")
        self.assertIn(self.client.post(url, {"rules": "on"}).status_code, {403, 404})
        self.standards.refresh_from_db()
        self.assertIsNone(self.standards.rules_by)
        page = self.client.get(reverse("knowledge-detail", args=[self.app.pk, self.standards.pk]))
        self.assertContains(page, 'id="rules"')
        self.assertNotContains(page, "Use as rules")

    def test_a_foreign_source_reads_as_not_found(self):
        ApplicationGrant.objects.create(application=self.other, user=self.owner, role="owner")
        foreign = add_knowledge(self.owner, self.other.pk, "Elsewhere", STANDARDS)
        url = reverse("knowledge-rules", args=[self.app.pk, foreign.pk])
        self.assertEqual(self.client.post(url, {"rules": "on"}).status_code, 404)

    def test_the_source_page_lists_what_it_is_read_as(self):
        self.mark()
        page = self.client.get(reverse("knowledge-detail", args=[self.app.pk, self.standards.pk]))
        self.assertContains(page, "Checked against")
        self.assertContains(page, "Collections are bounded at the server")
        self.assertContains(page, "Stop using as rules")

    def test_a_document_page_offers_it_and_returns_there(self):
        document = Document.objects.create(
            application=self.app, uploaded_by=self.owner, name="standards.md",
            size=len(STANDARDS), sha256=hashlib.sha256(STANDARDS.encode()).hexdigest(),
            status="ready", origin="upload",
        )
        KnowledgeEntry.objects.filter(pk=self.standards.pk).update(document=document)
        page = self.client.get(reverse("document-detail", args=[self.app.pk, document.pk]))
        self.assertContains(page, "Use as rules")
        response = self.client.post(
            reverse("knowledge-rules", args=[self.app.pk, self.standards.pk]),
            {"rules": "on", "next": "document"},
        )
        self.assertRedirects(
            response,
            reverse("document-detail", args=[self.app.pk, document.pk]) + "#rules",
            fetch_redirect_response=False,
        )

    def test_converting_again_keeps_the_owners_word(self):
        document = Document.objects.create(
            application=self.app, uploaded_by=self.owner, name="standards.md",
            size=len(STANDARDS), sha256=hashlib.sha256(STANDARDS.encode()).hexdigest(),
            status="ready", origin="upload",
        )
        entry = add_knowledge(
            self.owner, self.app.pk, "standards.md", STANDARDS, document=document
        )
        self.mark(entry)
        with transaction.atomic():
            second = supersede(self.owner, self.app.pk, document, STANDARDS + "\nOne more.\n")
        self.assertNotEqual(second.pk, entry.pk)
        self.assertEqual(second.rules_by, self.owner)
        self.assertIsNotNone(second.rules_at)

    # The plan review -----------------------------------------------------

    def plan(self, body):
        plan = ChangePlan.objects.create(
            application=self.app, author=self.owner, title="Stop logging names",
            proposal="p", validation="v", digest="d", status="proposed",
        )
        PlanItem.objects.create(
            plan=plan, sequence=1, category="stated", title="Log the patient id instead",
            explanation="e", change_summary="c",
        )
        FactoryRun.objects.create(
            application=self.app, requested_by=self.owner, number=1,
            ticket_title="Patient search logs names", ticket_body=body, plan=plan,
            status="planned", graph_version=1,
        )
        return plan

    def test_the_plan_review_shows_the_rules_that_apply_quoted(self):
        self.mark()
        plan = self.plan("Search writes the patient's name and email to the log on every call.")
        page = self.client.get(reverse("plan-detail", args=[self.app.pk, plan.pk]))
        self.assertContains(page, "Rules that apply")
        self.assertContains(page, "Nothing with a name goes in a log")
        self.assertContains(page, "Log the record&#x27;s id.")
        self.assertContains(page, "none of them approves or refuses anything")

    def test_a_plan_with_no_rules_marked_shows_no_card(self):
        plan = self.plan("Search writes the patient's name and email to the log on every call.")
        page = self.client.get(reverse("plan-detail", args=[self.app.pk, plan.pk]))
        self.assertNotContains(page, "Rules that apply")
