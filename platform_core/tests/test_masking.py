"""Personal data an owner chose to mask is replaced before a source is stored."""

import hashlib
import json
import tempfile
from pathlib import Path

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from platform_core.graphs import build_graph, sources_for
from platform_core.masking import (
    DEFAULT_KINDS,
    KINDS,
    key_name,
    key_source,
    mask,
    placeholder,
    remask,
    save_policy,
)
from platform_core.models import (
    ApplicationGrant,
    AuditEvent,
    Document,
    KnowledgeEntry,
    OrganizationMember,
    PersonalDataPolicy,
    User,
)
from platform_core.processing import supersede
from platform_core.sensitivity import SCAN_LIMIT, scan
from platform_core.workbench import add_knowledge

from . import test_documents

KEY = b"0" * 64
RECORD = (
    "Admission note\n"
    "MRN: RGH-0142857, admitted to ward 4.\n"
    "Patient name: Amara Okonjo\n"
    "NHS number 943 476 5919\n"
    "Contact amara.okonjo@example.org or +44 20 7946 0958."
)


class MaskTests(SimpleTestCase):
    def test_the_value_is_replaced_and_its_label_kept(self):
        masked, counts = mask("MRN: RGH-0142857, admitted.", DEFAULT_KINDS, KEY)
        self.assertRegex(masked, r"^MRN: \[MRN-[A-Z]{8}\], admitted\.$")
        self.assertEqual(counts, {"medical record number": 1})

    def test_a_placeholder_is_never_read_as_a_value(self):
        masked, _ = mask(RECORD, KINDS, KEY)
        self.assertEqual(scan(masked), {})
        # So masking twice changes nothing.
        self.assertEqual(mask(masked, KINDS, KEY), (masked, {}))

    def test_one_value_is_one_placeholder_however_it_is_written(self):
        first = placeholder(KEY, "medical record number", "RGH-0142857")
        self.assertEqual(first, placeholder(KEY, "medical record number", "rgh 0142857"))
        self.assertNotEqual(first, placeholder(KEY, "medical record number", "RGH-0142858"))
        self.assertNotEqual(first, placeholder(b"1" * 64, "medical record number", "RGH-0142857"))

    def test_only_the_chosen_kinds_are_masked(self):
        masked, counts = mask(RECORD, DEFAULT_KINDS, KEY)
        self.assertIn("amara.okonjo@example.org", masked)
        self.assertIn("+44 20 7946 0958", masked)
        self.assertNotIn("RGH-0142857", masked)
        self.assertNotIn("Amara Okonjo", masked)
        self.assertEqual(
            counts,
            {"medical record number": 1, "patient name": 1, "NHS number": 1},
        )

    def test_a_health_identifier_is_not_masked_again_as_a_telephone_number(self):
        masked, counts = mask("NHS number 943 476 5919", KINDS, KEY)
        self.assertEqual(counts, {"NHS number": 1})
        self.assertRegex(masked, r"^NHS number \[NHS-NUMBER-[A-Z]{8}\]$")

    def test_only_card_numbers_that_pass_the_checksum_are_masked(self):
        masked, counts = mask("Card 4111 1111 1111 1111, order 1234 5678 9012 3456.", KINDS, KEY)
        self.assertEqual(counts, {"payment card number": 1})
        self.assertIn("1234 5678 9012 3456", masked)

    def test_values_past_the_scan_limit_are_still_masked(self):
        masked, counts = mask("x" * SCAN_LIMIT + " MRN: RGH-0142857", DEFAULT_KINDS, KEY)
        self.assertEqual(counts, {"medical record number": 1})
        self.assertNotIn("RGH-0142857", masked)


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class MaskingAtStorageTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        self.mounted = tempfile.TemporaryDirectory()
        self.managed = tempfile.TemporaryDirectory()
        for folder in (self.mounted, self.managed):
            self.addCleanup(folder.cleanup)
        directories = override_settings(
            SECRET_DIRECTORY=self.mounted.name, MANAGED_SECRET_DIRECTORY=self.managed.name
        )
        directories.enable()
        self.addCleanup(directories.disable)

    def switch(self, enabled=True, kinds=DEFAULT_KINDS, user=None):
        self.client.force_login(
            user or self.owner, backend="django.contrib.auth.backends.ModelBackend"
        )
        data = {"purpose": "personal_data", "personal-kinds": list(kinds)}
        if enabled:
            data["personal-enabled"] = "on"
        return self.client.post(reverse("ai-settings", args=[self.app.pk]), data)

    def test_without_a_policy_text_is_stored_as_it_is(self):
        entry = add_knowledge(self.owner, self.app.pk, "Note", RECORD)
        self.assertEqual(entry.content, RECORD)
        self.assertEqual(entry.masked, {})

    def test_switching_it_on_writes_a_key_and_is_audited(self):
        self.assertEqual(self.switch().status_code, 302)
        policy = PersonalDataPolicy.objects.get(application=self.app)
        self.assertTrue(policy.enabled)
        self.assertEqual(policy.kinds, DEFAULT_KINDS)
        self.assertEqual(key_source(self.app.pk), "managed")
        self.assertEqual(len((Path(self.managed.name) / key_name(self.app.pk)).read_text()), 64)
        event = AuditEvent.objects.get(action="personal_data.policy_set")
        self.assertEqual(event.details, {"enabled": True, "kinds": DEFAULT_KINDS})

    def test_an_operator_key_wins_and_is_never_replaced(self):
        mounted = Path(self.mounted.name) / key_name(self.app.pk)
        mounted.write_text("operator-key")
        self.switch()
        self.assertEqual(key_source(self.app.pk), "operator")
        self.assertEqual(mounted.read_text(), "operator-key")
        self.assertFalse((Path(self.managed.name) / key_name(self.app.pk)).exists())

    def test_with_nowhere_to_write_a_key_it_is_refused(self):
        with override_settings(MANAGED_SECRET_DIRECTORY=""):
            response = self.switch()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, key_name(self.app.pk))
        self.assertFalse(PersonalDataPolicy.objects.filter(application=self.app).exists())

    def test_on_with_no_kind_is_refused(self):
        response = self.switch(kinds=[])
        self.assertContains(response, "Tick at least one kind")
        self.assertFalse(PersonalDataPolicy.objects.exists())

    def test_only_an_owner_sets_it(self):
        contributor = User.objects.create_user("contributor")
        OrganizationMember.objects.create(
            organization=self.app.product.portfolio.organization, user=contributor
        )
        ApplicationGrant.objects.create(application=self.app, user=contributor, role="contributor")
        self.assertEqual(self.switch(user=contributor).status_code, 403)
        self.assertFalse(PersonalDataPolicy.objects.exists())

    def test_the_stored_text_is_the_masked_text(self):
        self.switch()
        entry = add_knowledge(self.owner, self.app.pk, "Patient name: Amara Okonjo", RECORD)
        for value in ("RGH-0142857", "Amara Okonjo", "943 476 5919"):
            self.assertNotIn(value, entry.content)
            self.assertNotIn(value, entry.title)
        self.assertEqual(
            entry.masked, {"medical record number": 1, "patient name": 1, "NHS number": 1}
        )
        # What is still there is still suggested: PII was not ticked.
        self.assertEqual(entry.personal_data, {"email address": 1, "telephone number": 1})
        data, _ = build_graph(list(sources_for(self.app.pk)), 1)
        self.assertNotIn("RGH-0142857", json.dumps(data))

    def test_the_same_value_joins_across_sources(self):
        self.switch()
        first = add_knowledge(self.owner, self.app.pk, "A", "MRN: RGH-0142857")
        second = add_knowledge(self.owner, self.app.pk, "B", "Seen again, mrn rgh0142857.")
        token = first.content.split("MRN: ")[1]
        self.assertIn(token, second.content)

    def test_a_missing_key_stores_nothing(self):
        self.switch()
        (Path(self.managed.name) / key_name(self.app.pk)).unlink()
        with self.assertRaisesMessage(ValidationError, key_name(self.app.pk)):
            add_knowledge(self.owner, self.app.pk, "Note", RECORD)
        self.assertFalse(KnowledgeEntry.objects.exists())

    def test_switching_it_off_stores_text_as_it_is_again(self):
        self.switch()
        self.switch(enabled=False)
        entry = add_knowledge(self.owner, self.app.pk, "Note", RECORD)
        self.assertEqual(entry.content, RECORD)

    def test_converting_the_same_text_again_keeps_its_entry(self):
        self.switch()
        document = Document.objects.create(
            application=self.app, uploaded_by=self.owner, name="note.md", size=1, sha256="s"
        )
        entry = supersede(self.owner, self.app.pk, document, RECORD)
        again = supersede(self.owner, self.app.pk, document, RECORD)
        self.assertEqual(entry.pk, again.pk)
        self.assertTrue(again.active)

    def test_existing_sources_are_masked_in_place_retired_ones_too(self):
        current = add_knowledge(self.owner, self.app.pk, "Note", RECORD)
        retired = add_knowledge(self.owner, self.app.pk, "Old", "MRN: RGH-0000001")
        KnowledgeEntry.objects.filter(pk=retired.pk).update(active=False)
        save_policy(self.owner, self.app, True, DEFAULT_KINDS)
        changed, totals = remask(self.owner, self.app)
        self.assertEqual(changed, 2)
        self.assertEqual(totals["medical record number"], 2)
        current.refresh_from_db()
        retired.refresh_from_db()
        self.assertNotIn("RGH-0142857", current.content)
        self.assertNotIn("RGH-0000001", retired.content)
        self.assertEqual(current.digest, hashlib.sha256(current.content.encode()).hexdigest())
        self.assertEqual(current.masked["medical record number"], 1)
        self.assertEqual(remask(self.owner, self.app), (0, {}))
        self.assertTrue(AuditEvent.objects.filter(action="personal_data.remasked").exists())

    def test_the_mask_existing_button_needs_masking_on(self):
        self.client.force_login(self.owner, backend="django.contrib.auth.backends.ModelBackend")
        add_knowledge(self.owner, self.app.pk, "Note", RECORD)
        self.client.post(reverse("ai-settings", args=[self.app.pk]), {"purpose": "remask"})
        self.assertEqual(KnowledgeEntry.objects.get().content, RECORD)

    def test_the_pages_say_what_was_masked(self):
        self.switch()
        entry = add_knowledge(self.owner, self.app.pk, "Note", RECORD)
        settings_page = self.client.get(reverse("ai-settings", args=[self.app.pk]))
        self.assertContains(settings_page, "Masked when stored")
        source = self.client.get(reverse("knowledge-detail", args=[self.app.pk, entry.pk]))
        self.assertContains(source, "1 medical record number")
        self.assertContains(source, "tag-masked")
        sources = self.client.get(reverse("knowledge", args=[self.app.pk]))
        self.assertContains(sources, "tag-masked")
