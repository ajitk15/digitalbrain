"""The optional authenticator app: codes, sealing, and the step after a password."""

import time
from io import StringIO
from unittest import mock

from axes.utils import reset as axes_reset
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from platform_core import authenticator
from platform_core.models import AuditEvent, Authenticator, RecoveryCode, User

PASSWORD = "a long enough passphrase 42"


class CodeTests(TestCase):
    def test_codes_follow_rfc_6238(self):
        # RFC 6238 appendix B, SHA-1, T = 59 s, truncated to six digits.
        self.assertEqual(authenticator.code_at(b"12345678901234567890", 59 // 30), "287082")
        self.assertEqual(
            authenticator.code_at(b"12345678901234567890", 1111111109 // 30), "081804"
        )

    def test_a_seed_is_sealed_and_only_this_secret_key_opens_it(self):
        seed = b"x" * 20
        sealed = authenticator.seal(seed)
        self.assertNotIn("eHh4", sealed)
        self.assertEqual(authenticator.unseal(sealed), seed)
        with override_settings(SECRET_KEY="another key " * 6):
            self.assertIsNone(authenticator.unseal(sealed))
        from django.conf import settings

        with override_settings(
            SECRET_KEY="a rotated key " * 5, SECRET_KEY_FALLBACKS=[settings.SECRET_KEY]
        ):
            self.assertEqual(authenticator.unseal(sealed), seed)

    def test_the_link_names_the_issuer_and_the_key(self):
        user = User(username="ann")
        uri = authenticator.provisioning_uri(user, b"x" * 20)
        self.assertTrue(uri.startswith("otpauth://totp/Digital%20Brain%3Aann?"))
        self.assertIn("secret=" + authenticator.readable_key(b"x" * 20), uri)


@override_settings(
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class SignInTests(TestCase):
    def setUp(self):
        axes_reset()
        self.ann = User.objects.create_user("ann", password=PASSWORD)
        self.seed = authenticator.start(self.ann)
        self.codes = authenticator.confirm(self.ann, self.now())

    def now(self, offset=0):
        return authenticator.code_at(self.seed, authenticator.current_step() + offset)

    def password(self, username="ann"):
        return self.client.post(reverse("login"), {"username": username, "password": PASSWORD})

    def signed_in(self):
        return "_auth_user_id" in self.client.session

    def code(self, code):
        return self.client.post(reverse("sign-in-code"), {"code": code})

    def later(self, steps=2):
        # A code is accepted once; the next sign-in needs the next step's code.
        real = time.time
        return mock.patch(
            "platform_core.authenticator.time.time", lambda: real() + steps * 30
        )

    def test_turning_it_on_needs_a_code_from_the_app_and_gives_ten_recovery_codes(self):
        self.assertEqual(len(self.codes), 10)
        self.assertTrue(authenticator.enrolled(self.ann))
        self.assertEqual(RecoveryCode.objects.filter(user=self.ann).count(), 10)
        for code in self.codes:
            self.assertFalse(RecoveryCode.objects.filter(digest__contains=code).exists())
        self.assertTrue(AuditEvent.objects.filter(action="account.authenticator_added").exists())
        bob = User.objects.create_user("bob")
        authenticator.start(bob)
        self.assertIsNone(authenticator.confirm(bob, "000000"))
        self.assertFalse(authenticator.enrolled(bob))

    def test_a_password_alone_waits_for_the_code(self):
        response = self.password()
        self.assertRedirects(response, reverse("sign-in-code"), fetch_redirect_response=False)
        self.assertFalse(self.signed_in())
        with self.later():
            done = self.code(self.now())
        self.assertRedirects(done, "/", fetch_redirect_response=False)
        self.assertTrue(self.signed_in())
        self.assertNotIn(authenticator.PENDING_KEY, self.client.session)

    def test_a_code_is_never_accepted_twice(self):
        with self.later():
            code = self.now()
            self.password()
            self.code(code)
            self.assertTrue(self.signed_in())
            self.client.logout()
            self.password()
            again = self.code(code)
        self.assertEqual(again.status_code, 200)
        self.assertFalse(self.signed_in())

    def test_a_recovery_code_signs_in_once(self):
        self.password()
        self.code(self.codes[0].upper())
        self.assertTrue(self.signed_in())
        self.client.logout()
        self.password()
        self.code(self.codes[0])
        self.assertFalse(self.signed_in())
        self.assertEqual(authenticator.codes_left(self.ann), 9)

    def test_wrong_codes_send_the_person_back_to_the_password(self):
        self.password()
        for _ in range(authenticator.MAX_TRIES - 1):
            self.assertEqual(self.code("000000").status_code, 200)
        last = self.code("000000")
        self.assertRedirects(last, reverse("login"), fetch_redirect_response=False)
        self.assertNotIn(authenticator.PENDING_KEY, self.client.session)
        self.assertFalse(self.signed_in())

    def test_wrong_codes_count_toward_the_lockout(self):
        self.password()
        for _ in range(authenticator.MAX_TRIES):
            self.code("000000")
        self.password()
        self.code("000000")
        locked = self.code("000000")
        self.assertEqual(locked.status_code, 429)
        self.assertEqual(self.password().status_code, 429)
        self.assertFalse(self.signed_in())

    def test_the_code_page_without_a_password_is_nothing(self):
        response = self.client.get(reverse("sign-in-code"))
        self.assertRedirects(response, reverse("login"), fetch_redirect_response=False)

    def test_a_pending_password_expires(self):
        self.password()
        session = self.client.session
        session[authenticator.PENDING_KEY]["at"] -= authenticator.PENDING_SECONDS + 1
        session.save()
        with self.later():
            response = self.code(self.now())
        self.assertRedirects(response, reverse("login"), fetch_redirect_response=False)
        self.assertFalse(self.signed_in())

    def test_someone_without_an_app_signs_in_with_the_password(self):
        User.objects.create_user("bob", password=PASSWORD)
        response = self.password("bob")
        self.assertRedirects(response, "/", fetch_redirect_response=False)
        self.assertTrue(self.signed_in())

    def test_removing_it_on_their_own_screen_needs_a_code(self):
        self.client.force_login(self.ann, backend="django.contrib.auth.backends.ModelBackend")
        url = reverse("authenticator")
        self.assertContains(self.client.get(url), "10 recovery codes left")
        refused = self.client.post(url, {"action": "remove", "code": "000000"})
        self.assertEqual(refused.status_code, 200)
        self.assertTrue(authenticator.enrolled(self.ann))
        self.client.post(url, {"action": "remove", "code": self.codes[1]})
        self.assertFalse(authenticator.enrolled(self.ann))
        self.assertFalse(RecoveryCode.objects.filter(user=self.ann).exists())

    def test_setting_it_up_on_the_screen(self):
        bob = User.objects.create_user("bob")
        self.client.force_login(bob, backend="django.contrib.auth.backends.ModelBackend")
        url = reverse("authenticator")
        page = self.client.post(url, {"action": "start"})
        self.assertContains(page, "otpauth://totp/")
        seed = authenticator.pending_seed(bob)
        code = authenticator.code_at(seed, authenticator.current_step())
        done = self.client.post(url, {"action": "confirm", "code": code})
        self.assertContains(done, "Your recovery codes")
        self.assertTrue(authenticator.enrolled(bob))
        self.assertNotContains(self.client.get(url), "otpauth://")

    def test_new_recovery_codes_replace_the_old(self):
        self.client.force_login(self.ann, backend="django.contrib.auth.backends.ModelBackend")
        self.client.post(reverse("authenticator"), {"action": "codes", "code": self.codes[0]})
        self.assertEqual(authenticator.codes_left(self.ann), 10)
        self.assertIsNone(authenticator.verify(self.ann, self.codes[1]))

    def test_a_platform_administrator_removes_it_for_a_lost_phone(self):
        admin = User.objects.create_user("root", is_platform_admin=True)
        self.client.force_login(admin, backend="django.contrib.auth.backends.ModelBackend")
        page = self.client.get(reverse("person", args=[self.ann.pk]))
        self.assertContains(page, "Remove authenticator app")
        self.client.post(reverse("person-authenticator", args=[self.ann.pk]))
        self.assertFalse(Authenticator.objects.filter(user=self.ann).exists())
        event = AuditEvent.objects.get(action="account.authenticator_removed")
        self.assertEqual(event.actor, admin)

    def test_a_platform_administrators_app_is_removed_on_the_host(self):
        admin = User.objects.create_user("root", is_platform_admin=True)
        other = User.objects.create_user("root2", is_platform_admin=True)
        authenticator.start(admin)
        authenticator.confirm(admin, authenticator.code_at(
            authenticator.pending_seed(admin), authenticator.current_step()
        ))
        self.client.force_login(other, backend="django.contrib.auth.backends.ModelBackend")
        refused = self.client.post(reverse("person-authenticator", args=[admin.pk]))
        self.assertEqual(refused.status_code, 403)
        self.assertTrue(authenticator.enrolled(admin))
        call_command("remove_authenticator", "root", stdout=StringIO())
        self.assertFalse(authenticator.enrolled(admin))
