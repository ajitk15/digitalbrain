"""Sign in with Microsoft Entra ID, against a fake Microsoft.

No test reaches Microsoft. The fake signs ID tokens with an RSA key generated
here and publishes its public half as the tenant's key set, so every check in
`sso.verify` runs for real: the signature, issuer, audience, tenant, expiry and
nonce. Each refusal test changes exactly one thing about an otherwise valid
sign-in.
"""

import json
import tempfile
import time
import urllib.parse
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from digitalbrain import configuration
from platform_core import sso
from platform_core.fetching import FetchError
from platform_core.models import (
    Application,
    ApplicationGrant,
    AuditEvent,
    Organization,
    Portfolio,
    Product,
    SignInIdentity,
    User,
)

TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
OTHER_TENANT = "99999999-8888-7777-6666-555555555555"
OID = "0f0f0f0f-1111-2222-3333-444444444444"
SECRET = "test-client-secret-value"

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
STRANGER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwks():
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key()))
    public.update(kid="key-1", use="sig", alg="RS256")
    return {"keys": [public]}


class FakeMicrosoft:
    """Answers the token and key endpoints, and remembers what it was sent."""

    def __init__(self):
        self.claims = {}
        self.key = KEY
        self.headers = {"kid": "key-1"}
        self.algorithm = "RS256"
        self.token_status = None
        self.requests = []

    def id_token(self):
        now = int(time.time())
        claims = {
            "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
            "aud": CLIENT,
            "tid": TENANT,
            "oid": OID,
            "sub": "pairwise-subject",
            "iat": now,
            "nbf": now,
            "exp": now + 3600,
            "nonce": self.nonce,
            "preferred_username": "Dana@Contoso.com",
            "given_name": "Dana",
            "family_name": "Reyes",
        }
        claims.update(self.claims)
        claims = {k: v for k, v in claims.items() if v is not None}
        key = self.key if self.algorithm == "RS256" else "a-shared-secret-long-enough-for-hs256-x"
        return jwt.encode(claims, key, algorithm=self.algorithm, headers=self.headers)

    def __call__(self, url, *, headers=None, method="GET", body=None):
        self.requests.append((method, url, body))
        if url.endswith("/discovery/v2.0/keys"):
            return json.dumps(jwks()).encode(), "application/json", "keys", url
        if url.endswith("/oauth2/v2.0/token"):
            if self.token_status:
                raise FetchError("login.microsoftonline.com returned HTTP 400.", status=400)
            payload = {"id_token": self.id_token(), "access_token": "not-kept"}
            return json.dumps(payload).encode(), "application/json", "token", url
        raise AssertionError(f"unexpected fetch {url}")


@override_settings(
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    ENTRA_TENANT=TENANT,
    ENTRA_CLIENT_ID=CLIENT,
    ENTRA_CREATE_USERS=False,
)
class SignInTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.dana = User.objects.create_user(
            "dana", email="dana@contoso.com", password="not-a-real-password"
        )
        org = Organization.objects.create(name="Contoso")
        product = Product.objects.create(
            name="Care", portfolio=Portfolio.objects.create(name="Health", organization=org)
        )
        cls.app = Application.objects.create(name="CarePath", product=product)

    def setUp(self):
        self.secrets = tempfile.TemporaryDirectory()
        self.addCleanup(self.secrets.cleanup)
        secret = Path(self.secrets.name) / "entra_client_secret"
        secret.write_text(SECRET)
        secret.chmod(0o600)
        directory = override_settings(SECRET_DIRECTORY=self.secrets.name)
        directory.enable()
        self.addCleanup(directory.disable)
        self.microsoft = FakeMicrosoft()
        fetching = patch("platform_core.sso.fetch", self.microsoft)
        fetching.start()
        self.addCleanup(fetching.stop)
        sso.forget_keys()
        self.addCleanup(sso.forget_keys)

    def begin(self, next_url=""):
        address = reverse("sso-start")
        if next_url:
            address += f"?next={urllib.parse.quote(next_url)}"
        response = self.client.get(address)
        self.assertEqual(response.status_code, 302)
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(response["Location"]).query))
        self.microsoft.nonce = query["nonce"]
        return response, query

    def finish(self, **params):
        return self.client.get(reverse("sso-callback"), params)

    def sign_in(self, **params):
        _, query = self.begin()
        params.setdefault("code", "an-authorization-code")
        params.setdefault("state", query["state"])
        return self.finish(**params)

    def signed_in(self):
        return self.client.session.get("_auth_user_id")

    def assertRefused(self, response, words):
        self.assertRedirects(response, reverse("login"), fetch_redirect_response=False)
        self.assertIsNone(self.signed_in())
        page = self.client.get(reverse("login"))
        self.assertContains(page, words)

    # --- the button and the redirect ---------------------------------------

    def test_the_login_page_offers_microsoft_only_when_configured(self):
        self.assertContains(self.client.get(reverse("login")), "Sign in with Microsoft")
        with override_settings(ENTRA_TENANT="", ENTRA_CLIENT_ID=""):
            self.assertNotContains(self.client.get(reverse("login")), "Sign in with Microsoft")
            self.assertEqual(self.client.get(reverse("sso-start")).status_code, 404)
            self.assertEqual(self.client.get(reverse("sso-callback")).status_code, 404)

    def test_start_sends_the_browser_to_the_tenant_with_pkce(self):
        response, query = self.begin()
        location = urllib.parse.urlsplit(response["Location"])
        self.assertEqual(location.netloc, "login.microsoftonline.com")
        self.assertEqual(location.path, f"/{TENANT}/oauth2/v2.0/authorize")
        self.assertEqual(query["client_id"], CLIENT)
        self.assertEqual(query["response_type"], "code")
        self.assertEqual(query["code_challenge_method"], "S256")
        self.assertEqual(query["redirect_uri"], "http://testserver/accounts/sso/callback/")
        pending = self.client.session[sso.SESSION_KEY]
        self.assertEqual(query["state"], pending["state"])
        self.assertEqual(query["code_challenge"], sso._challenge(pending["verifier"]))
        # The verifier proves this server started the sign-in; it never leaves it.
        self.assertNotIn(pending["verifier"], response["Location"])

    def test_start_is_get_only(self):
        self.assertEqual(self.client.post(reverse("sso-start")).status_code, 405)

    # --- a good sign-in ------------------------------------------------------

    def test_first_sign_in_links_the_account_with_the_same_name(self):
        response = self.sign_in()
        self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)
        self.assertEqual(self.signed_in(), str(self.dana.pk))
        identity = SignInIdentity.objects.get(user=self.dana)
        self.assertEqual((identity.tenant, identity.subject), (TENANT, OID))
        self.assertEqual(identity.method, "name")
        self.assertEqual(identity.name, "dana@contoso.com")
        event = AuditEvent.objects.get(action="sso_linked")
        self.assertEqual(event.actor, self.dana)
        self.assertNotIn(SECRET, json.dumps(event.details))

    def test_the_code_is_traded_with_the_verifier_and_the_mounted_secret(self):
        self.sign_in()
        method, url, body = next(r for r in self.microsoft.requests if r[0] == "POST")
        self.assertEqual(url, f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token")
        sent = dict(urllib.parse.parse_qsl(body.decode()))
        self.assertEqual(sent["grant_type"], "authorization_code")
        self.assertEqual(sent["code"], "an-authorization-code")
        self.assertEqual(sent["client_secret"], SECRET)
        self.assertTrue(sent["code_verifier"])
        # Nothing of the exchange stays in the session afterwards.
        self.assertNotIn(sso.SESSION_KEY, self.client.session)

    def test_a_later_sign_in_matches_by_object_id_not_by_name(self):
        self.sign_in()
        self.client.logout()
        self.microsoft.claims = {"preferred_username": "dana.reyes@contoso.com"}
        self.sign_in()
        self.assertEqual(self.signed_in(), str(self.dana.pk))
        self.assertEqual(SignInIdentity.objects.get().name, "dana.reyes@contoso.com")
        self.assertEqual(AuditEvent.objects.filter(action="sso_linked").count(), 1)

    def test_next_is_followed_only_on_this_site(self):
        _, query = self.begin("/applications/")
        response = self.finish(code="c", state=query["state"])
        self.assertRedirects(response, "/applications/", fetch_redirect_response=False)
        self.client.logout()
        _, query = self.begin("https://evil.example/")
        response = self.finish(code="c", state=query["state"])
        self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)

    def test_signing_in_grants_no_application(self):
        self.sign_in()
        self.assertFalse(ApplicationGrant.objects.filter(user=self.dana).exists())
        response = self.client.get(reverse("application", args=[self.app.pk]))
        self.assertEqual(response.status_code, 404)

    # --- refusals: the callback itself ----------------------------------------

    def test_a_wrong_state_is_refused(self):
        self.begin()
        self.assertRefused(self.finish(code="c", state="forged"), "Try again")
        self.assertFalse(SignInIdentity.objects.exists())

    def test_a_callback_nobody_started_is_refused(self):
        self.assertRefused(self.finish(code="c", state="anything"), "Try again")

    def test_a_pending_sign_in_is_used_once(self):
        _, query = self.begin()
        self.finish(code="c", state=query["state"])
        self.client.logout()
        self.assertRefused(self.finish(code="c", state=query["state"]), "Try again")

    def test_an_old_pending_sign_in_is_refused(self):
        _, query = self.begin()
        session = self.client.session
        session[sso.SESSION_KEY]["at"] -= sso.PENDING_SECONDS + 1
        session.save()
        self.assertRefused(self.finish(code="c", state=query["state"]), "Try again")

    def test_microsofts_error_text_is_not_shown(self):
        _, query = self.begin()
        response = self.finish(
            error="server_error", error_description="<b>Call 555-0100</b>", state=query["state"]
        )
        self.assertRefused(response, "could not complete the sign-in")
        self.assertNotContains(self.client.get(reverse("login")), "555-0100")

    def test_a_cancelled_sign_in_says_so(self):
        _, query = self.begin()
        response = self.finish(error="access_denied", state=query["state"])
        self.assertRefused(response, "it was cancelled")

    def test_a_rejected_code_is_refused(self):
        self.microsoft.token_status = 400
        self.assertRefused(self.sign_in(), "could not complete the sign-in")

    def test_a_missing_client_secret_says_the_server_is_not_set_up(self):
        (Path(self.secrets.name) / "entra_client_secret").unlink()
        self.assertRefused(self.sign_in(), "not completely set up")

    # --- refusals: the token -------------------------------------------------

    def assertTokenRefused(self):
        self.assertRefused(self.sign_in(), "could not be verified")
        self.assertFalse(SignInIdentity.objects.exists())

    def test_a_token_signed_by_another_key_is_refused(self):
        self.microsoft.key = STRANGER_KEY
        self.assertTokenRefused()

    def test_an_unknown_key_id_is_refused(self):
        self.microsoft.headers = {"kid": "key-2"}
        self.assertTokenRefused()

    def test_a_symmetric_token_is_refused(self):
        self.microsoft.algorithm = "HS256"
        self.assertTokenRefused()

    def test_a_token_for_another_application_is_refused(self):
        self.microsoft.claims = {"aud": "12345678-1234-1234-1234-123456789012"}
        self.assertTokenRefused()

    def test_a_token_from_another_tenant_is_refused(self):
        self.microsoft.claims = {
            "iss": f"https://login.microsoftonline.com/{OTHER_TENANT}/v2.0",
            "tid": OTHER_TENANT,
        }
        self.assertTokenRefused()

    def test_a_token_whose_tenant_claim_disagrees_is_refused(self):
        self.microsoft.claims = {"tid": OTHER_TENANT}
        self.assertTokenRefused()

    def test_an_expired_token_is_refused(self):
        now = int(time.time())
        self.microsoft.claims = {"iat": now - 7200, "nbf": now - 7200, "exp": now - 3600}
        self.assertTokenRefused()

    def test_a_token_with_another_nonce_is_refused(self):
        self.microsoft.claims = {"nonce": "replayed-from-elsewhere"}
        self.assertTokenRefused()

    def test_a_token_without_an_object_id_is_refused(self):
        self.microsoft.claims = {"oid": None}
        self.assertTokenRefused()

    # --- refusals: who it is -------------------------------------------------

    def test_nobody_by_that_name_is_refused_by_default(self):
        self.microsoft.claims = {"preferred_username": "stranger@contoso.com"}
        self.assertRefused(self.sign_in(), "no account here")
        self.assertFalse(User.objects.filter(username="stranger@contoso.com").exists())

    @override_settings(ENTRA_CREATE_USERS=True)
    def test_a_created_account_has_no_password_and_no_access(self):
        self.microsoft.claims = {"preferred_username": "New.Person@contoso.com"}
        self.sign_in()
        user = User.objects.get(username="new.person@contoso.com")
        self.assertEqual(self.signed_in(), str(user.pk))
        self.assertFalse(user.has_usable_password())
        self.assertFalse(user.is_platform_admin)
        self.assertEqual((user.first_name, user.last_name), ("Dana", "Reyes"))
        self.assertFalse(user.organizationmember_set.exists())
        self.assertFalse(ApplicationGrant.objects.filter(user=user).exists())
        self.assertEqual(
            self.client.get(reverse("application", args=[self.app.pk])).status_code, 404
        )
        self.assertTrue(AuditEvent.objects.filter(action="sso_user_created", actor=user).exists())

    def test_a_platform_administrator_is_never_linked_by_name(self):
        self.dana.is_platform_admin = True
        self.dana.save()
        self.assertRefused(self.sign_in(), "linked to Microsoft by an operator")
        self.assertFalse(SignInIdentity.objects.exists())

    def test_an_account_linked_to_another_microsoft_account_is_not_taken(self):
        SignInIdentity.objects.create(
            user=self.dana, tenant=TENANT, subject="12345678-0000-0000-0000-000000000000",
            method="manual",
        )
        self.assertRefused(self.sign_in(), "already linked to a different Microsoft account")

    def test_two_matching_accounts_are_not_guessed_between(self):
        User.objects.create_user("dana@contoso.com", password="not-a-real-password")
        self.assertRefused(self.sign_in(), "More than one account")

    def test_a_deactivated_account_is_refused(self):
        self.sign_in()
        self.client.logout()
        self.dana.is_active = False
        self.dana.save()
        self.assertRefused(self.sign_in(), "deactivated")

    # --- the operator command ------------------------------------------------

    def test_an_operator_links_an_administrator_by_object_id(self):
        self.dana.is_platform_admin = True
        self.dana.save()
        call_command("link_sso", "dana", "--object-id", OID.upper(), stdout=StringIO())
        self.assertEqual(SignInIdentity.objects.get(user=self.dana).method, "manual")
        self.sign_in()
        self.assertEqual(self.signed_in(), str(self.dana.pk))

    def test_link_sso_refuses_an_account_someone_else_holds(self):
        other = User.objects.create_user("lee", password="not-a-real-password")
        call_command("link_sso", "lee", "--object-id", OID, stdout=StringIO())
        with self.assertRaises(CommandError):
            call_command("link_sso", "dana", "--object-id", OID, stdout=StringIO())
        call_command("link_sso", "lee", "--clear", stdout=StringIO())
        self.assertFalse(SignInIdentity.objects.filter(user=other).exists())
        with self.assertRaises(CommandError):
            call_command("link_sso", "dana", "--object-id", "common", stdout=StringIO())


class SignInConfigurationTests(SimpleTestCase):
    def load(self, extra):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                'mode = "development"\nhosts = ["localhost"]\n'
                f'secret_directory = "{Path(directory).as_posix()}"\n' + extra
            )
            with patch.dict("os.environ", {"DIGITAL_BRAIN_CONFIG": str(path)}):
                return configuration.load_config()

    def test_a_tenant_and_client_id_are_accepted(self):
        config = self.load(f'entra_tenant = "{TENANT}"\nentra_client_id = "{CLIENT}"\n')
        self.assertEqual(config["entra_tenant"], TENANT)

    def test_multi_tenant_names_are_refused(self):
        for tenant in ("common", "organizations", "consumers", "contoso.onmicrosoft.com"):
            with self.subTest(tenant=tenant), self.assertRaises(ImproperlyConfigured):
                self.load(f'entra_tenant = "{tenant}"\nentra_client_id = "{CLIENT}"\n')

    def test_half_a_configuration_is_refused(self):
        with self.assertRaises(ImproperlyConfigured):
            self.load(f'entra_tenant = "{TENANT}"\n')

    def test_the_client_secret_is_not_a_configuration_field(self):
        with self.assertRaises(ImproperlyConfigured):
            self.load('entra_client_secret = "pasted-here"\n')

    def test_create_users_must_be_a_boolean(self):
        with self.assertRaises(ImproperlyConfigured):
            self.load('entra_create_users = "yes"\n')
