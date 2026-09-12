"""Deny by default: administrative hierarchy never implies knowledge access."""

from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404

from .models import Application, ApplicationGrant, Organization, OrganizationMember, User


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
            active=True,
            grants__user=user,
            product__portfolio__organization__active=True,
            product__portfolio__organization_id__in=org_ids,
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
