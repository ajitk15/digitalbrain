"""A new application with no model credential, walked the way its owner meets it.

The first thing a new installation sees, and where three faults lived at once:
Generate pre-selected a model it had just said could not be reached, the run it
queued failed as "stopped unexpectedly", and Code Factory queued a run whose
plan-drafting model had no credential, then failed it the same way. Each page
already said why in a banner above; nothing acted on it.
"""

from django.test import TestCase, override_settings
from django.urls import reverse

from platform_core.ai import seed_application_ai
from platform_core.code_factory import start_run
from platform_core.graphs import publish_revision, rebuild
from platform_core.models import ChatMessage, FactoryRun, KnowledgeGraph
from platform_core.workbench import add_knowledge

from . import test_documents

NO_CREDENTIAL = "no Claude credential"


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    CLAUDE_USE_HOST_LOGIN=False,
)
class NoCredentialJourneyTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        # As the create form leaves it: every task on Claude, and no Claude
        # credential, because none was set up on this machine.
        seed_application_ai(self.owner, self.app, "claude")
        add_knowledge(
            self.owner,
            self.app.pk,
            "Escalation policy",
            "When the referral queue exceeds 200 items the on-call lead pages the duty manager.",
        )
        self.graph_url = reverse("graph", args=[self.app.pk])

    def test_the_owner_gets_a_working_structural_version_and_plain_refusals(self):
        # The Generate form offers what can work, and says why the model cannot.
        form = self.client.get(reverse("graph-generate", args=[self.app.pk]))
        self.assertContains(form, '<option value="structural" selected>')
        self.assertContains(form, NO_CREDENTIAL)

        # Asking for the model anyway is refused at the button; nothing queues.
        refused = self.client.post(
            self.graph_url,
            {"action": "generate", "model_choice": "claude:claude-sonnet-5"},
            follow=True,
        )
        self.assertContains(refused, NO_CREDENTIAL)
        self.assertFalse(KnowledgeGraph.objects.filter(status="queued").exists())

        # Structure alone works, and can be published.
        self.client.post(self.graph_url, {"action": "generate", "model_choice": "structural"})
        self.assertTrue(rebuild(self.app.pk))
        publish_revision(self.owner, self.app.pk, 1, reason="journey")

        # Chat in AI mode says what is missing and where it is set, not
        # "ask an owner" to the owner and not "stopped unexpectedly".
        # (Without JavaScript the question is answered in the request, and a
        # refusal is the form's error rather than a stored answer.)
        chat = self.client.post(
            reverse("chat", args=[self.app.pk]), {"question": "Who is paged?", "mode": "ai"}
        )
        self.assertContains(chat, NO_CREDENTIAL)
        self.assertContains(chat, "An owner can set one on Credentials")
        self.assertNotContains(chat, "Ask an application owner")
        self.assertFalse(ChatMessage.objects.exists())

        # Code Factory says so before anyone types a ticket, and refuses to queue.
        plans = self.client.get(reverse("plans", args=[self.app.pk]))
        self.assertContains(plans, "The model cannot be reached")
        with self.assertRaisesMessage(Exception, NO_CREDENTIAL):
            start_run(self.owner, self.app.pk, title="Cap page size", body="NFR-06 caps it.")
        self.assertFalse(FactoryRun.objects.exists())

    def test_sources_answers_still_work_without_any_model(self):
        self.client.post(self.graph_url, {"action": "generate", "model_choice": "structural"})
        rebuild(self.app.pk)
        self.client.post(
            reverse("chat", args=[self.app.pk]),
            {"question": "referral queue duty manager", "mode": "search"},
        )
        answer = ChatMessage.objects.filter(role="assistant").get()
        self.assertEqual(answer.status, "complete")
        self.assertIn("duty manager", answer.body)
