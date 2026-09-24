"""The chat endpoint: the one API surface that spends money.

What this pins, beyond the happy path:

* It is off until an owner turns it on. Every application that existed before
  the feature shipped has an explicit disabled row, and a new one starts with
  the box unticked, so no provider budget starts being spent by a release.
* A token is still a user. The same `access()` checks run, a revoked grant
  closes it, and a conversation belonging to somebody else reads as not found
  rather than as forbidden.
* Answering is the browser's own path. There is no second implementation to
  drift, and no way to pick a model or a credential through the API.
"""

import json
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from platform_core.api_auth import issue, reset_throttle
from platform_core.forms import ApplicationForm
from platform_core.models import (
    AIConfiguration,
    ApiToken,
    ApplicationFeature,
    ApplicationGrant,
    ChatConversation,
)
from platform_core.services import OPT_IN_FEATURES
from platform_core.utility import api_chat  # noqa: F401 - the surface under test lives here

from . import test_documents

SETTINGS = dict(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)


@override_settings(**SETTINGS)
class ChatApiTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        reset_throttle()
        self.url = reverse("api-chat", args=[self.app.pk])
        self.secret = self.mint(self.owner)
        AIConfiguration.objects.create(
            application=self.app,
            purpose="chat",
            provider="claude",
            model="claude-sonnet-5",
            enabled=True,
            input_rate=1,
            output_rate=5,
            configured_by=self.owner,
        )
        self.enable()

    def mint(self, user):
        prefix, secret, digest = issue()
        ApiToken.objects.create(
            application=self.app, user=user, name="test", prefix=prefix, digest=digest
        )
        return secret

    def enable(self, on=True):
        ApplicationFeature.objects.update_or_create(
            application=self.app, key="chat_api", defaults={"enabled": on}
        )

    def ask(self, secret=None, **body):
        headers = {"Authorization": f"Bearer {secret}"} if secret else {}
        return self.client.post(
            self.url, json.dumps(body), content_type="application/json", headers=headers
        )

    # ---- the switch that guards the budget ----

    def test_the_endpoint_is_off_until_an_owner_turns_it_on(self):
        """No application starts spending its provider budget on a release."""
        self.enable(False)
        with patch("platform_core.ai.invoke_ai") as model:
            response = self.ask(self.secret, question="What is Alpha?")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "chat_api_disabled")
        model.assert_not_called()

    def test_chat_api_starts_ticked_on_the_create_form(self):
        self.assertNotIn("chat_api", OPT_IN_FEATURES)
        org = self.app.product.portfolio.organization
        form = ApplicationForm(organization=org)
        self.assertTrue(form.fields["feature_chat_api"].initial)
        self.assertTrue(form.fields["feature_chat"].initial)
        # With no marker posted, the defaults stand - and those now include it.
        defaults = ApplicationForm(data={}, organization=org).selected_features()
        self.assertTrue(defaults["chat_api"])
        self.assertTrue(defaults["chat"])

    def test_api_access_shows_the_endpoint_and_says_when_it_is_off(self):
        """The address is worth knowing before it is switched on; hiding it
        would make the page look like the endpoint does not exist."""
        self.client.force_login(self.owner, backend="django.contrib.auth.backends.ModelBackend")
        page = reverse("api-tokens", args=[self.app.pk])
        address = reverse("api-chat", args=[self.app.slug])

        body = self.client.get(page).content.decode()
        self.assertIn(address, body)
        self.assertIn("Example request", body)
        # One tab strip for the three endpoints, sharing the behaviour class
        # the client tabs already use.
        self.assertIn('class="endpoint-tabs tabbed"', body)
        for label in ["tab-rest", "tab-mcp", "tab-chat"]:
            self.assertIn(f'id="{label}"', body)

        self.enable(False)
        body = self.client.get(page).content.decode()
        self.assertIn(address, body)
        self.assertIn("The Chat API is switched off", body)

    # ---- a token is still a user ----

    def test_an_unauthenticated_call_never_reaches_a_model(self):
        with patch("platform_core.ai.invoke_ai") as model:
            self.assertEqual(self.ask(question="Hello").status_code, 401)
            self.assertEqual(self.ask("nonsense", question="Hello").status_code, 401)
        model.assert_not_called()

    def test_a_withdrawn_grant_closes_the_endpoint(self):
        ApplicationGrant.objects.filter(application=self.app, user=self.owner).delete()
        with patch("platform_core.ai.invoke_ai") as model:
            response = self.ask(self.secret, question="What is Alpha?")
        self.assertIn(response.status_code, {403, 404})
        model.assert_not_called()

    def test_a_session_cookie_alone_cannot_reach_the_chat_api(self):
        self.client.force_login(self.owner, backend="django.contrib.auth.backends.ModelBackend")
        with patch("platform_core.ai.invoke_ai") as model:
            self.assertEqual(self.ask(question="What is Alpha?").status_code, 401)
        model.assert_not_called()

    def test_another_users_conversation_reads_as_not_found(self):
        """A token acts as its issuer, so it cannot extend somebody else's thread."""
        theirs = ChatConversation.objects.create(
            application=self.app, user=self.viewer, title="Theirs", mode="ai"
        )
        with patch("platform_core.ai.invoke_ai") as model:
            response = self.ask(
                self.secret, question="What is Alpha?", conversation_id=str(theirs.pk)
            )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")
        model.assert_not_called()

    # ---- answering ----

    def test_an_answer_comes_back_with_its_conversation_and_citations(self):
        with patch("platform_core.ai.invoke_ai", return_value="Alpha sends to Beta."):
            response = self.ask(self.secret, question="What is Alpha?")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["answer"], "Alpha sends to Beta.")
        self.assertEqual(body["mode"], "ai")
        self.assertIn("conversation_id", body)
        self.assertIsInstance(body["citations"], list)

    def test_a_follow_up_continues_the_same_conversation(self):
        with patch("platform_core.ai.invoke_ai", return_value="First."):
            first = self.ask(self.secret, question="What is Alpha?").json()
        with patch("platform_core.ai.invoke_ai", return_value="Second."):
            second = self.ask(
                self.secret, question="And Beta?", conversation_id=first["conversation_id"]
            ).json()
        self.assertEqual(second["conversation_id"], first["conversation_id"])
        conversation = ChatConversation.objects.get(pk=first["conversation_id"])
        self.assertEqual(conversation.messages.count(), 4)

    def test_a_follow_up_cannot_move_the_thread_onto_another_mode(self):
        """Sending a message never changes what answers a thread, as in the browser."""
        with patch("platform_core.ai.invoke_ai", return_value="First."):
            first = self.ask(self.secret, question="What is Alpha?", mode="ai").json()
        with patch("platform_core.ai.invoke_ai", return_value="Second."):
            second = self.ask(
                self.secret,
                question="And Beta?",
                conversation_id=first["conversation_id"],
                mode="graph",
            ).json()
        self.assertEqual(second["mode"], "ai")

    def test_a_bad_request_is_refused_before_a_model_is_called(self):
        for body in [{}, {"question": "  "}, {"question": 7}, {"question": "x", "mode": "free"}]:
            with self.subTest(body=body):
                with patch("platform_core.ai.invoke_ai") as model:
                    response = self.ask(self.secret, **body)
                self.assertEqual(response.status_code, 400)
                model.assert_not_called()

    def test_streaming_returns_a_handle_rather_than_an_answer(self):
        response = self.ask(self.secret, question="What is Alpha?", stream=True)
        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertIn("message_id", body)
        self.assertEqual(
            body["stream_url"],
            reverse("api-chat-stream", args=[str(self.app.pk), body["message_id"]]),
        )

    def test_search_never_streams_because_streaming_would_call_a_model(self):
        """`answer_worker` has no lexical branch, so streaming search would bill
        a caller for the one mode that is free. The browser only streams ai and
        graph for the same reason; search answers synchronously instead."""
        response = self.ask(self.secret, question="Alpha", mode="search", stream=True)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertNotIn("stream_url", body)
        self.assertIn("answer", body)
        self.assertEqual(body["mode"], "search")

    def test_the_stream_refuses_a_message_that_is_not_this_users(self):
        response = self.ask(self.secret, question="What is Alpha?", stream=True).json()
        other = self.mint(self.viewer)
        stream = self.client.get(
            response["stream_url"], headers={"Authorization": f"Bearer {other}"}
        )
        self.assertIn(stream.status_code, {403, 404})
