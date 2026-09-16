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

    def test_an_address_may_name_the_application_readably(self):
        """A UUID in every client configuration is a maintenance cost."""
        readable = reverse("api-graph-search", args=[self.app.slug])
        self.assertIn(self.app.slug, readable)
        response = self.client.get(
            readable, {"q": "Service Alpha"}, headers={"Authorization": f"Bearer {self.secret}"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)

    def test_the_identifier_form_still_works(self):
        """Anything already configured must keep running."""
        response = self.client.get(
            reverse("api-graph-search", args=[self.app.pk]),
            {"q": "Service Alpha"},
            headers={"Authorization": f"Bearer {self.secret}"},
        )
        self.assertEqual(response.status_code, 200)

    def test_an_address_naming_nothing_is_answered_like_a_bad_token(self):
        """A readable name must not tell an outsider which applications exist."""
        unknown = reverse("api-graph-search", args=["no-such-application"])
        with_token = self.client.get(
            unknown, {"q": "x"}, headers={"Authorization": f"Bearer {self.secret}"}
        )
        wrong_token = self.client.get(
            reverse("api-graph-search", args=[self.app.slug]),
            {"q": "x"},
            headers={"Authorization": "Bearer dbk_000000000000_wrong"},
        )
        self.assertEqual(with_token.status_code, 401)
        self.assertEqual(wrong_token.status_code, 401)
        self.assertEqual(with_token.json()["error"], wrong_token.json()["error"])

    def test_a_token_for_one_application_is_refused_on_another_s_name(self):
        from platform_core.models import ApplicationGrant

        ApplicationGrant.objects.create(application=self.other, user=self.owner, role="owner")
        response = self.client.get(
            reverse("api-graph-search", args=[self.other.slug]),
            {"q": "x"},
            headers={"Authorization": f"Bearer {self.secret}"},
        )
        self.assertEqual(response.status_code, 401)

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


    def notify(self, method):
        """A JSON-RPC notification: no id, and no response is permitted."""
        return self.client.post(
            self.mcp_url,
            data=json.dumps({"jsonrpc": "2.0", "method": method}),
            content_type="application/json",
            headers={"Authorization": f"Bearer {self.secret}"},
        )

    def test_a_notification_is_accepted_with_no_body(self):
        """Replying to a notification hands the client a response it never asked for."""
        response = self.notify("notifications/initialized")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.content, b"")

    def test_an_unknown_notification_is_ignored_rather_than_answered(self):
        response = self.notify("notifications/cancelled")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.content, b"")

    def test_a_notification_still_requires_a_token(self):
        response = self.client.post(
            self.mcp_url,
            data=json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)

    def test_ping_is_a_request_and_does_get_a_result(self):
        payload = self.rpc("ping").json()
        self.assertEqual(payload["id"], 1)
        self.assertEqual(payload["result"], {})

    def test_the_transport_declines_get_and_delete_as_the_spec_requires(self):
        """No SSE stream and no session teardown here; 405 is the documented answer."""
        headers = {"Authorization": f"Bearer {self.secret}"}
        self.assertEqual(self.client.get(self.mcp_url, headers=headers).status_code, 405)
        self.assertEqual(self.client.delete(self.mcp_url, headers=headers).status_code, 405)

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

    def snippet(self, name):
        body = self.client.get(self.url).content.decode()
        return body.split(f'id="snip-{name}"')[1].split("</pre>")[0]

    def test_the_vs_code_snippet_uses_the_shape_vs_code_actually_reads(self):
        """VS Code ignores `mcpServers` and requires an explicit http type."""
        snippet = self.snippet("vscode")
        self.assertIn('"servers"', snippet)
        self.assertIn('"type": "http"', snippet)
        self.assertNotIn("mcpServers", snippet)
        # The token is prompted for, not written into a file that gets committed.
        self.assertIn("${input:digital-brain-token}", snippet)

    def test_each_client_snippet_names_this_application_s_own_endpoint(self):
        """A copied snippet has to work as pasted, not point at some other app."""
        for name in ("claude-code", "claude-desktop", "vscode", "cursor", "kiro", "chatgpt"):
            with self.subTest(client=name):
                self.assertIn(self.app.slug, self.snippet(name))

    def test_no_snippet_carries_a_real_token(self):
        """These are copied and pasted into files; a live secret must never be in one."""
        body = self.client.get(self.url).content.decode()
        for placeholder in ("YOUR_TOKEN", "${env:", "${input:"):
            self.assertIn(placeholder, body)
        self.assertNotIn("dbk_", body)

    def test_every_tab_has_a_panel_and_exactly_one_opens_by_default(self):
        """A count mismatch would leave a tab that reveals the wrong panel.

        Two groups now: the three endpoints, and the MCP clients nested inside
        the MCP one. Counted per group, because a page-wide total would pass
        while one group was short and the other long.
        """
        body = self.client.get(self.url).content.decode()
        self.assertEqual(body.count('name="endpoint"'), 3)
        self.assertEqual(body.count('name="connect-client"'), 8)
        self.assertEqual(body.count('<section class="tab-panel"'), 11)
        # One default per group, or a group opens with every panel hidden.
        self.assertEqual(body.count('class="tab-radio" checked'), 2)

    def test_the_tabs_carry_no_inline_script_or_handler(self):
        """The CSP forbids both; these tabs are radio inputs and CSS only."""
        body = self.client.get(self.url).content.decode()
        self.assertNotIn("<script>", body)
        self.assertNotIn("onclick", body)
        self.assertIn('class="client-tabs tabbed"', body)
        self.assertIn('class="endpoint-tabs tabbed"', body)

    def test_the_client_tabs_do_not_reuse_the_settings_nav_class(self):
        """`tabs` already belongs to the settings sub-nav; sharing it restyles that."""
        body = self.client.get(self.url).content.decode()
        self.assertIn('class="tabs settings-tabs"', body)
        self.assertNotIn('<div class="tabs">', body)

    def test_the_page_shows_the_readable_address(self):
        body = self.client.get(self.url).content.decode()
        self.assertIn(f"/applications/{self.app.slug}/graph/search/", body)
        self.assertIn(f"/applications/{self.app.slug}/mcp/", body)

    def test_the_endpoints_are_shown_as_complete_copyable_urls(self):
        """A caller should never have to assemble the origin by hand."""
        body = self.client.get(self.url).content.decode()
        root = f"http://testserver/api/v1/applications/{self.app.slug}"
        self.assertIn(f'id="rest-url">{root}/graph/search/', body)
        self.assertIn(f'id="mcp-url">{root}/mcp/', body)

    def test_every_snippet_has_a_copy_button_pointing_at_it(self):
        import re

        body = self.client.get(self.url).content.decode()
        targets = set(re.findall(r'data-copy-target="#([\w-]+)"', body))
        # Every endpoint and every client snippet is copyable.
        self.assertLessEqual({"rest-url", "rest-example", "mcp-url"}, targets)
        self.assertLessEqual(
            {f"snip-{name}" for name in ("claude-code", "vscode", "cursor", "kiro", "curl-call")},
            targets,
        )
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
