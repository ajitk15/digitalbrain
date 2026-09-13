import json

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from platform_core.api_auth import digest_of, issue, reset_throttle
from platform_core.models import ApiToken, ApplicationGrant, FeatureSwitch, GraphRevision
from platform_core.workbench import add_knowledge

from . import test_documents


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class GraphApiTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        reset_throttle()
        self.source = add_knowledge(
            self.owner, self.app.pk, "Architecture", "Service Alpha sends messages to Queue Beta."
        )
        self.publish(1)
        self.secret = self.mint(self.owner)
        self.url = reverse("api-graph-search", args=[self.app.pk])
        self.mcp_url = reverse("api-mcp", args=[self.app.pk])

    def publish(self, number):
        quote = "Service Alpha sends messages to Queue Beta."
        revision = GraphRevision.objects.create(
            application=self.app,
            number=number,
            fingerprint="f",
            published_at=timezone.now(),
            data={
                "nodes": [
                    {"id": "a", "label": "Service Alpha", "kind": "entity"},
                    {"id": "b", "label": "Queue Beta", "kind": "entity"},
                ],
                "edges": [
                    {
                        "source": "a",
                        "target": "b",
                        "relation": "sends messages to",
                        "knowledge_id": str(self.source.pk),
                        "digest": self.source.digest,
                        "evidence": quote,
                        "line": 1,
                    }
                ],
                "sources": [{"id": str(self.source.pk), "digest": self.source.digest}],
            },
            quality={},
        )
        return revision

    def mint(self, user, **overrides):
        prefix, secret, digest = issue()
        ApiToken.objects.create(
            application=self.app,
            user=user,
            name="test",
            prefix=prefix,
            digest=digest,
            **overrides,
        )
        return secret

    def call(self, secret=None, **params):
        headers = {"Authorization": f"Bearer {secret}"} if secret else {}
        return self.client.get(self.url, params, headers=headers)

    # ---- authentication ----

    def test_a_valid_token_returns_verified_evidence_and_its_version(self):
        response = self.call(self.secret, q="Service Alpha")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["count"], 1)
        self.assertIn("sends messages to", payload["results"][0]["evidence"])

    def test_no_token_is_refused(self):
        self.assertEqual(self.call(None, q="x").status_code, 401)

    def test_a_wrong_secret_with_a_real_prefix_is_refused(self):
        token = ApiToken.objects.get()
        forged = f"{token.prefix}_not-the-real-secret"
        self.assertEqual(self.call(forged, q="x").status_code, 401)

    def test_a_session_cookie_alone_cannot_reach_the_api(self):
        """Accepting cookies here would make this endpoint CSRF-able."""
        self.client.force_login(self.owner, backend="django.contrib.auth.backends.ModelBackend")
        self.assertEqual(self.client.get(self.url, {"q": "x"}).status_code, 401)

    def test_a_revoked_token_stops_working(self):
        ApiToken.objects.update(revoked_at=timezone.now())
        self.assertEqual(self.call(self.secret, q="x").status_code, 401)

    def test_an_expired_token_stops_working(self):
        ApiToken.objects.update(expires_at=timezone.now() - timezone.timedelta(days=1))
        self.assertEqual(self.call(self.secret, q="x").status_code, 401)

    def test_the_secret_is_never_stored(self):
        token = ApiToken.objects.get()
        self.assertNotIn(self.secret, token.digest)
        self.assertEqual(token.digest, digest_of(self.secret))
        self.assertEqual(len(token.digest), 64)

    # ---- authorization: the platform's rules, not a second set ----

    def test_revoking_the_users_grant_closes_the_token(self):
        ApplicationGrant.objects.filter(application=self.app, user=self.owner).delete()
        self.assertIn(self.call(self.secret, q="x").status_code, {403, 404})

    def test_disabling_the_feature_closes_the_token(self):
        FeatureSwitch.objects.create(key="knowledge", enabled=False)
        self.assertEqual(self.call(self.secret, q="x").status_code, 403)

    def test_deactivating_the_user_closes_the_token(self):
        self.owner.is_active = False
        self.owner.save(update_fields=["is_active"])
        self.assertEqual(self.call(self.secret, q="x").status_code, 401)

    def test_a_token_cannot_be_used_on_another_application(self):
        ApplicationGrant.objects.create(application=self.other, user=self.owner, role="owner")
        other_url = reverse("api-graph-search", args=[self.other.pk])
        response = self.client.get(
            other_url, {"q": "x"}, headers={"Authorization": f"Bearer {self.secret}"}
        )
        self.assertEqual(response.status_code, 401)

    def test_a_viewers_token_can_read_but_reaches_no_further(self):
        secret = self.mint(self.viewer)
        self.assertEqual(self.call(secret, q="Service Alpha").status_code, 200)

    # ---- versions ----

    def test_a_version_can_be_pinned(self):
        self.publish(2)
        self.assertEqual(self.call(self.secret, q="Service Alpha", version=1).json()["version"], 1)
        self.assertEqual(self.call(self.secret, q="Service Alpha").json()["version"], 2)

    def test_an_unpublished_draft_is_never_answered_from(self):
        GraphRevision.objects.create(
            application=self.app, number=9, fingerprint="f", data={}, quality={}
        )
        self.assertEqual(self.call(self.secret, q="Service Alpha").json()["version"], 1)

    def test_a_bad_version_is_a_clear_error_not_a_500(self):
        self.assertEqual(self.call(self.secret, q="x", version="abc").status_code, 400)
        self.assertEqual(self.call(self.secret, q="x", version=99).status_code, 409)

    def test_a_missing_question_is_refused(self):
        self.assertEqual(self.call(self.secret, q="").status_code, 400)

    # ---- what leaves the building ----

    def test_the_source_digest_is_never_returned(self):
        body = self.call(self.secret, q="Service Alpha").content.decode()
        self.assertNotIn(self.source.digest, body)
        self.assertNotIn("digest", body)

    # ---- MCP ----

    def rpc(self, method, secret=None, **params):
        return self.client.post(
            self.mcp_url,
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}),
            content_type="application/json",
            headers={"Authorization": f"Bearer {secret or self.secret}"},
        )

    def test_initialize_advertises_tools(self):
        payload = self.rpc("initialize").json()
        self.assertEqual(payload["jsonrpc"], "2.0")
        self.assertIn("tools", payload["result"]["capabilities"])

    def test_tools_list_describes_the_search_tool(self):
        tools = self.rpc("tools/list").json()["result"]["tools"]
        self.assertEqual(tools[0]["name"], "search_knowledge_graph")
        self.assertIn("version", tools[0]["inputSchema"]["properties"])

    def test_a_tool_call_returns_structured_evidence(self):
        result = self.rpc(
            "tools/call",
            name="search_knowledge_graph",
            arguments={"question": "Service Alpha"},
        ).json()["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"]["count"], 1)

    def test_a_tool_failure_is_a_readable_result_not_a_protocol_error(self):
        """The model should be able to read and react to the problem."""
        result = self.rpc(
            "tools/call", name="search_knowledge_graph", arguments={"question": ""}
        ).json()["result"]
        self.assertTrue(result["isError"])
        self.assertIn("question", result["content"][0]["text"].lower())

    def test_an_unknown_tool_is_a_protocol_error(self):
        self.assertEqual(self.rpc("tools/call", name="rm_rf").json()["error"]["code"], -32602)

    def test_an_unknown_method_is_reported(self):
        self.assertEqual(self.rpc("nonsense").json()["error"]["code"], -32601)

    def test_mcp_requires_a_token_too(self):
        response = self.client.post(
            self.mcp_url,
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)

    def test_malformed_json_is_a_parse_error(self):
        response = self.client.post(
            self.mcp_url,
            data="{not json",
            content_type="application/json",
            headers={"Authorization": f"Bearer {self.secret}"},
        )
        self.assertEqual(response.json()["error"]["code"], -32700)

    # ---- throttling ----

    def test_a_token_is_rate_limited(self):
        from platform_core import api_auth

        with override_settings():
            api_auth.RATE_LIMIT, original = 3, api_auth.RATE_LIMIT
            try:
                for _ in range(3):
                    self.assertEqual(self.call(self.secret, q="Service Alpha").status_code, 200)
                self.assertEqual(self.call(self.secret, q="Service Alpha").status_code, 429)
            finally:
                api_auth.RATE_LIMIT = original
                reset_throttle()


@override_settings(
    DOCUMENT_AUTO_CONVERT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class TokenManagementTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)
        reset_throttle()
        self.url = reverse("api-tokens", args=[self.app.pk])

    def test_a_new_token_is_shown_once_and_stored_only_as_a_hash(self):
        response = self.client.post(self.url, {"action": "create", "name": "CI", "expires": "90"})
        self.assertEqual(response.status_code, 200)
        token = ApiToken.objects.get()
        secret = response.context["created"]
        self.assertTrue(secret.startswith("dbk_"))
        self.assertEqual(token.digest, digest_of(secret))
        # Revisiting the page must not show it again.
        self.assertIsNone(self.client.get(self.url).context["created"])

    def test_a_token_can_be_revoked_and_then_stops_working(self):
        self.client.post(self.url, {"action": "create", "name": "CI", "expires": "0"})
        token = ApiToken.objects.get()
        self.client.post(self.url, {"action": "revoke", "token": str(token.pk)})
        token.refresh_from_db()
        self.assertIsNotNone(token.revoked_at)
        self.assertFalse(token.active)

    def test_a_user_cannot_revoke_someone_elses_token(self):
        prefix, _, digest = issue()
        other = ApiToken.objects.create(
            application=self.app, user=self.viewer, name="theirs", prefix=prefix, digest=digest
        )
        response = self.client.post(self.url, {"action": "revoke", "token": str(other.pk)})
        self.assertEqual(response.status_code, 404)
        other.refresh_from_db()
        self.assertIsNone(other.revoked_at)

    def test_the_endpoints_are_shown_as_complete_copyable_urls(self):
        """A caller should never have to assemble the origin by hand."""
        body = self.client.get(self.url).content.decode()
        root = f"http://testserver/api/v1/applications/{self.app.pk}"
        self.assertIn(f'id="rest-url">{root}/graph/search/', body)
        self.assertIn(f'id="mcp-url">{root}/mcp/', body)

    def test_every_snippet_has_a_copy_button_pointing_at_it(self):
        import re

        body = self.client.get(self.url).content.decode()
        targets = set(re.findall(r'data-copy-target="#([\w-]+)"', body))
        self.assertEqual(targets, {"rest-url", "rest-example", "mcp-url", "mcp-config"})
        # A button whose target is missing stays hidden, so the ids must exist.
        for target in targets:
            self.assertIn(f'id="{target}"', body)

    def test_a_revealed_token_can_be_copied(self):
        response = self.client.post(self.url, {"action": "create", "name": "CI", "expires": "0"})
        body = response.content.decode()
        self.assertIn('data-copy-target="#token-secret"', body)
        self.assertIn(f'id="token-secret">{response.context["created"]}', body)

    def test_only_your_own_tokens_are_listed(self):
        prefix, _, digest = issue()
        ApiToken.objects.create(
            application=self.app, user=self.viewer, name="theirs", prefix=prefix, digest=digest
        )
        self.assertNotContains(self.client.get(self.url), "theirs")
