"""Owners invite; an organization administrator decides who joins."""

import re

from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.test import TestCase, override_settings
from django.urls import reverse

from platform_core import invitations
from platform_core.forms import AcceptInvitationForm
from platform_core.models import (
    Application,
    ApplicationGrant,
    AuditEvent,
    Invitation,
    Organization,
    OrganizationMember,
    Portfolio,
    Product,
    User,
)

PASSWORD = "a long enough passphrase 42"


@override_settings(
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class InvitationTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner")
        self.admin = User.objects.create_user("admin")
        self.member = User.objects.create_user("member")
        self.outsider = User.objects.create_user("outsider", email="out@example.com")
        self.org = Organization.objects.create(name="Northstar")
        OrganizationMember.objects.create(organization=self.org, user=self.owner)
        OrganizationMember.objects.create(organization=self.org, user=self.member)
        OrganizationMember.objects.create(organization=self.org, user=self.admin, is_admin=True)
        portfolio = Portfolio.objects.create(name="Commerce", organization=self.org)
        product = Product.objects.create(name="Payments", portfolio=portfolio)
        self.app = Application.objects.create(name="Refunds", product=product)
        self.owner_grant = ApplicationGrant.objects.create(
            application=self.app, user=self.owner, role="owner", can_approve=True
        )
        self.access = reverse("application-access", args=[self.app.pk])
        self.members = reverse("organization-members", args=[self.org.pk])

    def login(self, user):
        self.client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")

    def grant(self, user):
        return ApplicationGrant.objects.filter(application=self.app, user=user).first()

    def is_member(self, user):
        return OrganizationMember.objects.filter(organization=self.org, user=user).exists()

    def invite_new_person(self):
        return invitations.invite(self.owner, self.app.pk, "new@example.com", "contributor")

    def test_someone_already_in_the_organization_is_given_access_at_once(self):
        self.assertIsNone(invitations.invite(self.owner, self.app.pk, "member", "viewer"))
        self.assertEqual(self.grant(self.member).role, "viewer")
        self.assertFalse(Invitation.objects.exists())

    def test_an_account_outside_the_organization_waits_for_its_administrator(self):
        invitation = invitations.invite(self.owner, self.app.pk, "out@example.com", "viewer")
        self.assertEqual(invitation.status, Invitation.Status.AWAITING)
        self.assertEqual(invitation.user, self.outsider)
        self.assertFalse(self.is_member(self.outsider))
        self.assertIsNone(self.grant(self.outsider))

        self.assertIsNone(invitations.approve(self.admin, invitation.pk))
        invitation.refresh_from_db()
        self.assertEqual(invitation.status, Invitation.Status.ACCEPTED)
        self.assertEqual(invitation.decided_by, self.admin)
        self.assertTrue(self.is_member(self.outsider))
        self.assertEqual(self.grant(self.outsider).role, "viewer")

    def test_a_new_person_gets_a_link_that_makes_their_account_once(self):
        invitation = self.invite_new_person()
        secret = invitations.approve(self.admin, invitation.pk)
        self.assertTrue(secret)
        invitation.refresh_from_db()
        self.assertEqual(invitation.status, Invitation.Status.OPEN)
        self.assertNotIn(secret, invitation.link_digest)

        url = reverse("invitation-accept", args=[invitation.pk, secret])
        self.assertContains(self.client.get(url), "Join Refunds")
        made = self.client.post(
            url,
            {
                "username": "newcomer",
                "first_name": "New",
                "last_name": "Comer",
                "password1": PASSWORD,
                "password2": PASSWORD,
            },
        )
        self.assertRedirects(made, reverse("login"), fetch_redirect_response=False)
        newcomer = User.objects.get(username="newcomer")
        self.assertEqual(newcomer.email, "new@example.com")
        self.assertTrue(newcomer.check_password(PASSWORD))
        self.assertTrue(self.is_member(newcomer))
        self.assertEqual(self.grant(newcomer).role, "contributor")
        invitation.refresh_from_db()
        self.assertEqual(invitation.status, Invitation.Status.ACCEPTED)
        self.assertEqual(invitation.link_digest, "")

        again = self.client.get(url)
        self.assertContains(again, "no longer works")
        self.assertNotContains(again, "password1")

    def test_a_wrong_or_expired_secret_is_no_link(self):
        invitation = self.invite_new_person()
        secret = invitations.approve(self.admin, invitation.pk)
        self.assertIsNone(invitations.opened(invitation.pk, secret + "x"))
        Invitation.objects.filter(pk=invitation.pk).update(link_expires_at="2000-01-01T00:00Z")
        self.assertIsNone(invitations.opened(invitation.pk, secret))

    def test_a_new_link_replaces_the_one_before(self):
        invitation = self.invite_new_person()
        first = invitations.approve(self.admin, invitation.pk)
        second = invitations.issue_link(self.owner, invitation.pk)
        self.assertIsNone(invitations.opened(invitation.pk, first))
        self.assertIsNotNone(invitations.opened(invitation.pk, second))

    def test_withdrawn_or_declined_invitations_let_nobody_in(self):
        waiting = invitations.invite(self.owner, self.app.pk, "outsider", "viewer")
        invitations.decline(self.admin, waiting.pk)
        with self.assertRaises(ValidationError):
            invitations.approve(self.admin, waiting.pk)
        self.assertFalse(self.is_member(self.outsider))

        invitation = self.invite_new_person()
        secret = invitations.approve(self.admin, invitation.pk)
        invitations.withdraw(self.owner, invitation.pk)
        self.assertIsNone(invitations.opened(invitation.pk, secret))

    def test_an_owner_who_lost_the_application_cannot_have_it_approved(self):
        invitation = self.invite_new_person()
        self.owner_grant.role = "viewer"
        self.owner_grant.save()
        with self.assertRaises(ValidationError):
            invitations.approve(self.admin, invitation.pk)

    def test_the_link_is_refused_once_the_inviter_lost_the_application(self):
        invitation = self.invite_new_person()
        secret = invitations.approve(self.admin, invitation.pk)
        self.owner_grant.delete()
        form = AcceptInvitationForm(
            {"username": "newcomer", "password1": PASSWORD, "password2": PASSWORD}
        )
        self.assertTrue(form.is_valid())
        with self.assertRaises(ValidationError):
            invitations.accept(invitation.pk, secret, form)
        self.assertFalse(User.objects.filter(username="newcomer").exists())

    def test_only_an_organization_administrator_decides(self):
        invitation = self.invite_new_person()
        for person in (self.owner, self.member):
            with self.assertRaises(Http404):
                invitations.approve(person, invitation.pk)
        self.login(self.owner)
        response = self.client.post(
            self.members, {"action": "approve", "invitation": invitation.pk}
        )
        self.assertEqual(response.status_code, 404)
        invitation.refresh_from_db()
        self.assertEqual(invitation.status, Invitation.Status.AWAITING)

    def test_an_owner_who_administers_the_organization_approves_by_inviting(self):
        OrganizationMember.objects.filter(user=self.owner).update(is_admin=True)
        invitation = invitations.invite(self.owner, self.app.pk, "outsider", "viewer")
        self.assertEqual(invitation.status, Invitation.Status.ACCEPTED)
        self.assertTrue(self.is_member(self.outsider))

    def test_the_same_person_is_not_invited_twice(self):
        self.invite_new_person()
        with self.assertRaises(ValidationError):
            invitations.invite(self.owner, self.app.pk, "NEW@example.com", "viewer")

    def test_approval_rights_are_given_only_by_someone_who_holds_them(self):
        self.owner_grant.can_approve = False
        self.owner_grant.save()
        with self.assertRaises(PermissionDenied):
            invitations.invite(self.owner, self.app.pk, "new@example.com", "viewer", True)

    def test_a_non_owner_cannot_invite(self):
        ApplicationGrant.objects.create(application=self.app, user=self.member, role="contributor")
        with self.assertRaises(PermissionDenied):
            invitations.invite(self.member, self.app.pk, "new@example.com", "viewer")

    def test_the_screens_carry_an_invitation_from_owner_to_administrator(self):
        self.login(self.owner)
        sent = self.client.post(
            self.access, {"action": "invite", "invitee": "outsider", "role": "viewer"}
        )
        self.assertRedirects(sent, self.access)
        page = self.client.get(self.access)
        self.assertContains(page, "Waiting for approval")
        self.assertContains(page, 'id="id_role"', count=1)

        self.login(self.admin)
        page = self.client.get(self.members)
        self.assertContains(page, "Waiting for your approval")
        invitation = Invitation.objects.get()
        approved = self.client.post(
            self.members, {"action": "approve", "invitation": invitation.pk}
        )
        self.assertRedirects(approved, self.members)
        self.assertEqual(self.grant(self.outsider).role, "viewer")

    def test_a_link_is_shown_once_to_the_administrator_who_approved(self):
        invitation = self.invite_new_person()
        self.login(self.admin)
        self.client.post(self.members, {"action": "approve", "invitation": invitation.pk})
        shown = self.client.get(self.members).content.decode()
        found = re.search(r"/invitations/[0-9a-f-]+/([^/<]+)/", shown)
        self.assertIsNotNone(found)
        self.assertIsNotNone(invitations.opened(invitation.pk, found.group(1)))
        self.assertNotIn(found.group(1), self.client.get(self.members).content.decode())

    def test_every_step_is_audited_without_the_secret(self):
        invitation = self.invite_new_person()
        secret = invitations.approve(self.admin, invitation.pk)
        actions = set(AuditEvent.objects.values_list("action", flat=True))
        self.assertTrue(
            {"invitation.created", "invitation.approved", "invitation.link_issued"} <= actions
        )
        for details in AuditEvent.objects.values_list("details", flat=True):
            self.assertNotIn(secret, str(details))
