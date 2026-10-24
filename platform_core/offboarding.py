"""Offboarding: someone leaves, and a named successor takes over what they held.

Disabling an account is refused while it is the last owner of an application
or the last administrator of an organization, rightly - nobody could manage
either afterwards. So someone leaving needed a platform administrator to
promote a replacement application by application before the disable would go
through. Offboarding does it in one audited step, on the person's page:

* the successor becomes an owner of every application the leaver owned, with
  approval rights if the leaver held them (a successor's own are never
  lowered), and an administrator of every organization the leaver
  administered. The successor must already be a member of each organization:
  who joins one is still its administrators' decision.
* connectors the leaver created in those applications run as the successor
  from now on, so a scheduled import does not stop the day they leave.
* automatic AI refresh the leaver switched on is stopped with a note, not
  moved: it spends the application's budget as whoever switched it on, and
  the successor has not asked to pay for it.
* the account is disabled and its API tokens revoked, as a disable does.
  Grants stay, for an account that is enabled again.

A platform administrator may not name themselves: administering the platform
grants no application access, and this must not be the way round that. Nor
is a platform administrator offboarded here - the host does that, as it
resets their passwords.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from .api_auth import revoke_all
from .models import ApplicationGrant, Connector, KnowledgeGraph, OrganizationMember, User
from .services import audit


def holdings(leaver):
    """What offboarding `leaver` would hand over, for the confirmation page."""
    owned = (
        ApplicationGrant.objects.filter(user=leaver, role="owner")
        .select_related("application__product__portfolio__organization")
        .order_by("application__name")
    )
    return {
        "applications": owned,
        "organizations": OrganizationMember.objects.filter(user=leaver, is_admin=True)
        .select_related("organization")
        .order_by("organization__name"),
        "connectors": Connector.objects.filter(
            created_by=leaver, application_id__in=owned.values("application_id")
        ).count(),
        "refreshes": KnowledgeGraph.objects.filter(auto_refresh_by=leaver).count(),
    }


@transaction.atomic
def offboard(actor, leaver_pk, successor_username):
    """Hand `leaver`'s ownership to the successor, then disable them. Returns a summary."""
    if not actor.is_platform_admin:
        raise PermissionDenied
    leaver = User.objects.select_for_update().get(pk=leaver_pk)
    if leaver.is_platform_admin:
        raise PermissionDenied("A platform administrator is offboarded on the host.")
    if not leaver.is_active:
        raise ValidationError("This account is already disabled.")
    successor = User.objects.filter(username=successor_username.strip(), is_active=True).first()
    if successor is None:
        raise ValidationError("Name an active user ID as the successor.")
    if successor == leaver:
        raise ValidationError("Name someone other than the person leaving.")
    if successor == actor:
        raise ValidationError(
            "Name someone other than yourself: administering the platform gives no "
            "application access, and this would."
        )
    held = holdings(leaver)
    joined = set(
        OrganizationMember.objects.filter(user=successor).values_list("organization_id", flat=True)
    )
    needed = {g.application.organization_id: g.application.product.portfolio.organization.name
              for g in held["applications"]}
    needed.update((m.organization_id, m.organization.name) for m in held["organizations"])
    outside = sorted(name for pk, name in needed.items() if pk not in joined)
    if outside:
        raise ValidationError(
            f"{successor.username} is not a member of {', '.join(outside)}. An organization "
            "administrator adds them first."
        )
    app_ids = []
    for grant in held["applications"]:
        app = grant.application
        app_ids.append(app.pk)
        existing = ApplicationGrant.objects.filter(application=app, user=successor).first()
        can_approve = grant.can_approve or bool(existing and existing.can_approve)
        ApplicationGrant.objects.update_or_create(
            application=app, user=successor, defaults={"role": "owner", "can_approve": can_approve}
        )
        audit(
            actor,
            "application.access_changed",
            successor.pk,
            app.product.portfolio.organization,
            details={
                "application_id": str(app.pk),
                "role": "owner",
                "can_approve": can_approve,
                "reason": "successor at offboarding",
                "offboarded": str(leaver.pk),
            },
        )
    for member in held["organizations"]:
        OrganizationMember.objects.filter(
            organization=member.organization, user=successor
        ).update(is_admin=True)
        audit(
            actor,
            "organization.admin_added",
            successor.pk,
            member.organization,
            details={"reason": "successor at offboarding", "offboarded": str(leaver.pk)},
        )
    connectors = Connector.objects.filter(created_by=leaver, application_id__in=app_ids).update(
        created_by=successor
    )
    refreshes = KnowledgeGraph.objects.filter(auto_refresh_by=leaver).update(
        auto_refresh_by=None,
        auto_refresh_note=(
            f"Automatic refresh stopped: {leaver.username} left. An owner can switch it on "
            "again, and pays for it."
        ),
    )
    leaver.is_active = False
    leaver.save(update_fields=["is_active"])
    tokens = revoke_all(leaver, actor, reason="account offboarded")
    summary = {
        "successor": str(successor.pk),
        "applications": len(app_ids),
        "organizations": len(held["organizations"]),
        "connectors": connectors,
        "refreshes_stopped": refreshes,
        "tokens": tokens,
    }
    audit(actor, "account.offboarded", leaver.pk, details=summary)
    return {**summary, "successor": successor}
