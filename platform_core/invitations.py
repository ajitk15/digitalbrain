"""Inviting someone to an application, with their organization's approval.

Getting one person into one application took three people and an exchange
outside the platform: a platform administrator made the account and handed
over a password, an organization administrator typed the exact user ID onto
Members, and an owner granted the role. Now the owner starts it on the Access
screen, by user ID or email:

* Someone already in the organization gets the grant at once - an owner could
  always do that.
* Anyone else is an `Invitation` waiting on the organization's administrator,
  because who joins an organization is still theirs alone. An owner who also
  administers the organization approves by asking.
* Approved, an existing account becomes a member with its grant. A person
  with no account gets a one-time link (`LINK_HOURS`) to make one - the
  password is theirs, and nobody hands one over. Only the link's digest is
  stored, compared with `hmac.compare_digest`, as an API token's is.

The inviting owner's rights are checked again when it is approved and when
the link is used: an invitation asks for nothing its owner could not still
give, and approval rights only from an owner who holds them.
"""

import hashlib
import hmac
import secrets
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone

from .models import Application, ApplicationGrant, Invitation, OrganizationMember, User
from .policy import application_for, organization_for
from .services import audit, change_grant

#: How long a link to make an account works once issued.
LINK_HOURS = 72
SECRET_BYTES = 32
PENDING = (Invitation.Status.AWAITING, Invitation.Status.OPEN)


def digest_of(secret):
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def find_account(text):
    """The one account a user ID or email names, or None. Several is refused."""
    found = User.objects.filter(username=text).first()
    if found is not None:
        return found
    if "@" not in text:
        return None
    matches = list(User.objects.filter(email__iexact=text)[:2])
    if len(matches) > 1:
        raise ValidationError("Several accounts use that email address. Enter the user ID.")
    return matches[0] if matches else None


def organization_of(invitation):
    return invitation.application.product.portfolio.organization


def is_organization_admin(user, organization_id):
    return OrganizationMember.objects.filter(
        organization_id=organization_id, user=user, is_admin=True, user__is_active=True
    ).exists()


@transaction.atomic
def invite(actor, application_id, invitee, role, can_approve=False):
    """Ask for `invitee` to get `role`. Returns the invitation, or None when granted now."""
    if role not in ApplicationGrant.Role.values:
        raise ValidationError("Invalid application role.")
    Application.objects.select_for_update().get(pk=application_id)
    app, actor_grant = application_for(actor, application_id, owner=True)
    if can_approve and not actor_grant.can_approve:
        raise PermissionDenied("You cannot delegate approval rights you do not hold.")
    invitee = invitee.strip()
    if not invitee:
        raise ValidationError("Enter a user ID or an email address.")
    user = find_account(invitee)
    if user is not None and not user.is_active:
        raise ValidationError("That account is disabled. A platform administrator can enable it.")
    if user is not None and OrganizationMember.objects.filter(
        organization_id=app.organization_id, user=user
    ).exists():
        change_grant(actor, app.pk, user, role, can_approve)
        return None
    waiting = Invitation.objects.filter(application=app, status__in=PENDING)
    if (waiting.filter(user=user) if user else waiting.filter(invitee__iexact=invitee)).exists():
        raise ValidationError("That person has already been invited to this application.")
    invitation = Invitation.objects.create(
        application=app,
        invitee=invitee,
        user=user,
        role=role,
        can_approve=can_approve,
        invited_by=actor,
    )
    organization = app.product.portfolio.organization
    audit(
        actor,
        "invitation.created",
        invitation.pk,
        organization,
        details={
            "application_id": str(app.pk),
            "role": role,
            "can_approve": can_approve,
            "existing_account": user is not None,
        },
    )
    if is_organization_admin(actor, organization.pk):
        approve(actor, invitation.pk)
        invitation.refresh_from_db()
    return invitation


def still_asked_for(invitation):
    """Refuse an invitation its owner could no longer give."""
    grant = ApplicationGrant.objects.filter(
        application=invitation.application,
        user=invitation.invited_by,
        role="owner",
        user__is_active=True,
    ).first()
    if grant is None:
        raise ValidationError("The owner who sent this invitation no longer owns the application.")
    if invitation.can_approve and not grant.can_approve:
        raise ValidationError("The owner who sent this no longer holds approval rights to give.")


def admit(invitation, user, actor):
    """Make `user` a member with the invitation's grant, audited as `actor`."""
    organization = organization_of(invitation)
    _, joined = OrganizationMember.objects.get_or_create(organization=organization, user=user)
    if joined:
        audit(
            actor,
            "organization.member_added",
            user.pk,
            organization,
            details={"invitation": str(invitation.pk)},
        )
    ApplicationGrant.objects.update_or_create(
        application=invitation.application,
        user=user,
        defaults={"role": invitation.role, "can_approve": invitation.can_approve},
    )
    audit(
        actor,
        "application.access_changed",
        user.pk,
        organization,
        details={
            "application_id": str(invitation.application_id),
            "role": invitation.role,
            "can_approve": invitation.can_approve,
            "invitation": str(invitation.pk),
        },
    )


def pending_for(actor, organization_pk):
    """Invitations waiting on this organization administrator."""
    organization_for(actor, organization_pk, admin=True)
    return (
        Invitation.objects.filter(
            status=Invitation.Status.AWAITING,
            application__product__portfolio__organization_id=organization_pk,
        )
        .select_related("application", "invited_by", "user")
        .order_by("created_at")
    )


def _decision(actor, pk):
    invitation = get_object_or_404(
        Invitation.objects.select_for_update(of=("self",)).select_related(
            "application__product__portfolio__organization", "invited_by", "user"
        ),
        pk=pk,
    )
    organization_for(actor, organization_of(invitation).pk, admin=True)
    if invitation.status != Invitation.Status.AWAITING:
        raise ValidationError("This invitation has already been decided.")
    return invitation


@transaction.atomic
def approve(actor, pk):
    """Let the invitee in. Returns a link secret when they still need an account."""
    invitation = _decision(actor, pk)
    still_asked_for(invitation)
    invitation.decided_by = actor
    invitation.decided_at = timezone.now()
    organization = organization_of(invitation)
    if invitation.user is not None:
        if not invitation.user.is_active:
            raise ValidationError("That account has been disabled since it was invited.")
        admit(invitation, invitation.user, actor)
        invitation.status = Invitation.Status.ACCEPTED
        invitation.accepted_at = invitation.decided_at
        invitation.save()
        audit(actor, "invitation.approved", invitation.pk, organization)
        return None
    invitation.status = Invitation.Status.OPEN
    invitation.save()
    audit(actor, "invitation.approved", invitation.pk, organization)
    return issue_link(actor, invitation.pk)


@transaction.atomic
def decline(actor, pk):
    invitation = _decision(actor, pk)
    invitation.status = Invitation.Status.DECLINED
    invitation.decided_by = actor
    invitation.decided_at = timezone.now()
    invitation.save()
    audit(actor, "invitation.declined", invitation.pk, organization_of(invitation))


def _for_owner_or_admin(actor, pk):
    invitation = get_object_or_404(
        Invitation.objects.select_for_update(of=("self",)).select_related(
            "application__product__portfolio__organization"
        ),
        pk=pk,
    )
    organization = organization_of(invitation)
    if not is_organization_admin(actor, organization.pk):
        application_for(actor, invitation.application_id, owner=True)
    return invitation


@transaction.atomic
def withdraw(actor, pk):
    invitation = _for_owner_or_admin(actor, pk)
    if invitation.status not in PENDING:
        raise ValidationError("Only an invitation still waiting can be withdrawn.")
    invitation.status = Invitation.Status.WITHDRAWN
    invitation.link_digest = ""
    invitation.save()
    audit(actor, "invitation.withdrawn", invitation.pk, organization_of(invitation))


@transaction.atomic
def issue_link(actor, pk):
    """A new one-time link for an approved invitation; any earlier one stops working."""
    invitation = _for_owner_or_admin(actor, pk)
    if invitation.status != Invitation.Status.OPEN:
        raise ValidationError("A link is only issued once the invitation is approved.")
    secret = secrets.token_urlsafe(SECRET_BYTES)
    invitation.link_digest = digest_of(secret)
    invitation.link_expires_at = timezone.now() + timedelta(hours=LINK_HOURS)
    invitation.save(update_fields=["link_digest", "link_expires_at"])
    audit(actor, "invitation.link_issued", invitation.pk, organization_of(invitation))
    return secret


def opened(pk, secret):
    """The invitation a link names, if the link still works; otherwise None."""
    invitation = (
        Invitation.objects.filter(pk=pk, status=Invitation.Status.OPEN)
        .select_related("application__product__portfolio__organization", "invited_by")
        .first()
    )
    if (
        invitation is None
        or not invitation.link_digest
        or not invitation.link_expires_at
        or invitation.link_expires_at <= timezone.now()
        or not hmac.compare_digest(invitation.link_digest, digest_of(secret))
    ):
        return None
    return invitation


@transaction.atomic
def accept(pk, secret, form):
    """Make the account a link asked for, and let it in. Returns the new user."""
    invitation = opened(pk, secret)
    if invitation is None:
        raise ValidationError("This invitation link has been used, withdrawn or has expired.")
    Invitation.objects.select_for_update().get(pk=invitation.pk)
    still_asked_for(invitation)
    user = form.save(commit=False)
    if "@" in invitation.invitee and not user.email:
        user.email = invitation.invitee
    try:
        with transaction.atomic():
            user.save()
    except IntegrityError:
        raise ValidationError("That user ID is taken. Choose another.") from None
    organization = organization_of(invitation)
    audit(
        invitation.decided_by or invitation.invited_by,
        "account.created",
        user.pk,
        details={"invitation": str(invitation.pk)},
    )
    admit(invitation, user, user)
    invitation.user = user
    invitation.status = Invitation.Status.ACCEPTED
    invitation.accepted_at = timezone.now()
    invitation.link_digest = ""
    invitation.save()
    audit(user, "invitation.accepted", invitation.pk, organization)
    return user
