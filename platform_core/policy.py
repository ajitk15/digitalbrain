"""Deny by default: administrative hierarchy never implies knowledge access."""

from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone

from .models import Application, ApplicationGrant, Organization, OrganizationMember, User

#: An application is live while it and every level above it are on
#: (`hierarchy.py`). Each level keeps its own flag, so turning a parent back on
#: restores exactly what was on.
LIVE = {
    "active": True,
    "product__active": True,
    "product__portfolio__active": True,
    "product__portfolio__organization__active": True,
}


#: What each application role may do, in the words the Access and My access
#: screens use. A closed registry like `services.FEATURES`: one entry per role
#: and one for approval rights, and `test_capabilities` holds each claim to
#: what `workbench.access` and `application_for(owner=True)` actually allow, so
#: the screens cannot promise what the checks refuse.
CAPABILITIES = {
    "viewer": (
        "Ask questions in Chat and read every answer's sources",
        "Read knowledge, the Code Graph, Code Factory plans and ServiceOps",
    ),
    "contributor": (
        "Everything a viewer can",
        "Add, import and change knowledge and connectors' records",
        "Start Code Factory runs, triage incidents and request reviews",
    ),
    "owner": (
        "Everything a contributor can",
        "Give, change and revoke access, and invite people",
        "Set AI providers, credentials, budgets, features and connectors",
    ),
    "can_approve": ("Approve Code Factory plans, and give approval rights to others",),
}


def require_platform_admin(user):
    if not User.objects.filter(pk=user.pk, is_active=True, is_platform_admin=True).exists():
        raise PermissionDenied


def organizations_for(user):
    return Organization.objects.filter(active=True, members__user=user)


def organization_for(user, pk, *, admin=False):
    query = organizations_for(user)
    if admin:
        query = query.filter(members__user=user, members__is_admin=True)
    return get_object_or_404(query.distinct(), pk=pk)


def applications_for(user):
    if not User.objects.filter(pk=user.pk, is_active=True).exists():
        return Application.objects.none()
    org_ids = OrganizationMember.objects.filter(user=user).values("organization_id")
    return (
        Application.objects.filter(
            # Every level up the tree on (`hierarchy.LIVE`): a disabled
            # portfolio or product hides what is under it, as an organization does.
            **LIVE,
            product__portfolio__organization_id__in=org_ids,
        )
        # One filter call, so the expiry is read on the same grant as the user:
        # a grant past its `expires_at` opens nothing, before the maintenance
        # lane has revoked it (`access_review.expire_due`).
        .filter(
            Q(grants__user=user)
            & (Q(grants__expires_at__isnull=True) | Q(grants__expires_at__gt=timezone.now()))
        )
        .select_related("product__portfolio__organization")
        .distinct()
    )


def application_for(user, pk, *, owner=False):
    app = get_object_or_404(applications_for(user), pk=pk)
    grant = ApplicationGrant.objects.get(application=app, user=user)
    if owner and grant.role != ApplicationGrant.Role.OWNER:
        raise PermissionDenied
    return app, grant
