"""Regression checks for validation and navigation after the presentation changes."""

from html.parser import HTMLParser

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from platform_core.models import (
    AIConfiguration,
    Document,
    FeatureSwitch,
    GraphRevision,
    KnowledgeEntry,
)
from platform_core.source_library import graph_usage
from platform_core.workbench import add_knowledge

from . import test_documents


class FormDisclosures(HTMLParser):
    """Record whether each form's enclosing disclosures expose its errors."""

    def __init__(self, html):
        super().__init__()
        self.disclosures = []
        self.forms = []
        self.current = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "details":
            self.disclosures.append("open" in attrs)
        elif tag == "form":
            self.current = {"visible": all(self.disclosures), "fields": {}}
            self.forms.append(self.current)
        elif tag == "input" and self.current is not None:
            self.current["fields"][attrs.get("name")] = attrs.get("value", "")

    def handle_endtag(self, tag):
        if tag == "details":
            self.disclosures.pop()
        elif tag == "form":
            self.current = None


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class RedesignWorkflowTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)

    def test_library_combines_documents_and_records_without_duplicate_content(self):
        doc = Document.objects.create(
            application=self.app, uploaded_by=self.owner, name="Guide.pdf", size=100,
            sha256="a" * 64, status="ready",
        )
        entry = add_knowledge(self.owner, self.app.pk, "Converted guide", "Unique search phrase")
        entry.document = doc
        entry.save(update_fields=["document"])
        add_knowledge(self.owner, self.app.pk, "Imported ticket", "Ticket details")
        Document.objects.create(
            application=self.app, uploaded_by=self.owner, name="Failed.pdf", size=100,
            sha256="b" * 64, status="failed",
        )
        url = reverse("documents", args=[self.app.pk])
        response = self.client.get(url)
        self.assertEqual(response.context["page"].paginator.count, 3)
        self.assertContains(response, "Imported ticket")
        self.assertContains(response, "Conversion failed")
        self.assertContains(response, "View searchable content")
        self.assertEqual(self.client.get(url, {"q": "Unique search phrase"})
                         .context["page"].paginator.count, 1)
        self.assertEqual(self.client.get(reverse("knowledge", args=[self.app.pk]))
                         .context["page"].paginator.count, 3)
        entry.active = False
        entry.save(update_fields=["active"])
        self.assertContains(self.client.get(url), "No active searchable content")
        self.assertEqual(self.client.get(url, {"q": "Unique search phrase"})
                         .context["page"].paginator.count, 0)

    def test_graph_usage_matches_digest_and_application_and_labels_publication(self):
        entry = add_knowledge(self.owner, self.app.pk, "Guide", "Evidence")
        source = {"id": str(entry.pk), "digest": entry.digest}
        for app, number, digest, published in [
            (self.app, 1, entry.digest, True), (self.app, 2, entry.digest, False),
            (self.app, 3, "old-content", False), (self.other, 4, entry.digest, True),
        ]:
            GraphRevision.objects.create(
                application=app, number=number, fingerprint="f" * 64,
                data={"sources": [{**source, "digest": digest}]},
                published_at=timezone.now() if published else None,
            )
        self.assertEqual(graph_usage(self.app, [entry])[str(entry.pk)], [
            {"number": 2, "published": False}, {"number": 1, "published": True},
        ])
        response = self.client.get(reverse("documents", args=[self.app.pk]))
        self.assertContains(response, "v2 · Draft")
        self.assertContains(response, "v1 · Published")
        self.assertNotContains(response, "?version=3")
        self.assertNotContains(response, "?version=4")
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse("documents", args=[self.app.pk])).status_code, 404)

    def test_disabled_knowledge_keeps_documents_but_hides_searchable_records(self):
        add_knowledge(self.owner, self.app.pk, "Private record", "Evidence")
        FeatureSwitch.objects.create(key="knowledge", enabled=False)
        response = self.client.get(reverse("documents", args=[self.app.pk]))
        self.assertNotContains(response, "Private record")
        self.assertEqual(response.context["page"].paginator.count, 0)

    def test_no_matches_does_not_claim_the_application_has_no_sources(self):
        add_knowledge(self.owner, self.app.pk, "Architecture", "Service Alpha uses Queue Beta.")
        url = reverse("knowledge", args=[self.app.pk])
        response = self.client.get(url, {"q": "not-a-matching-source"})
        self.assertContains(response, "No matching sources")
        self.assertNotContains(response, "No sources yet")
        self.assertContains(response, "clear the search")
        self.assertEqual(KnowledgeEntry.objects.filter(application=self.app).count(), 1)
        self.assertContains(self.client.get(url), "Architecture")

    def test_manual_source_creation_is_unavailable_even_with_a_direct_post(self):
        url = reverse("knowledge", args=[self.app.pk])
        page = self.client.get(url)
        self.assertNotContains(page, "Add source")
        self.assertNotContains(page, "Save source")
        response = self.client.post(
            url, {"title": "Manual note", "content": "This must not be created."}
        )
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.headers["Allow"], "GET")
        self.assertFalse(KnowledgeEntry.objects.exists())

    def test_invalid_ai_save_exposes_only_its_form_without_saving(self):
        response = self.client.post(
            reverse("ai-settings", args=[self.app.pk]),
            {
                "purpose": "chat",
                "chat-model_choice": "custom",
                "chat-provider": "openai",
                "chat-model": "my-custom-model",
                "chat-input_rate": "not-a-number",
                "chat-output_rate": "1",
            },
        )
        self.assertEqual(response.status_code, 200)
        forms = FormDisclosures(response.content.decode()).forms
        chat = next(form for form in forms if form["fields"].get("purpose") == "chat")
        self.assertTrue(chat["visible"])
        self.assertEqual(chat["fields"]["chat-model"], "my-custom-model")
        self.assertTrue(all(
            not form["visible"] for form in forms
            if form["fields"].get("purpose") not in (None, "chat")
        ))
        self.assertFalse(AIConfiguration.objects.filter(application=self.app).exists())

    def test_failed_upload_is_exposed_without_javascript(self):
        response = self.client.post(
            reverse("documents", args=[self.app.pk]),
            {"file": [SimpleUploadedFile(f"file-{i}.txt", b"example") for i in range(21)]},
        )
        self.assertEqual(response.status_code, 200)
        forms = FormDisclosures(response.content.decode()).forms
        upload = next(form for form in forms if "file" in form["fields"])
        self.assertTrue(upload["visible"])
        self.assertFalse(Document.objects.exists())
