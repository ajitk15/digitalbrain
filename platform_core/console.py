"""What the platform administration pages read: attention, sign-in, operations.

The console used to be one page that listed organizations and AI spend, so a
platform administrator found out an organization had lost its last
administrator, or a lane had stopped, only by going to look. `attention` is
the list of what needs someone, worked out on each visit from the rows the
platform keeps - counts and links, never a record's content - and the
Overview leads with it. Like the rest of the console it opens no
application: an application with no owner is named and counted, and fixing it
is its organization's Structure or Members page.
"""

from django.conf import settings
from django.db.models import Count, Exists, OuterRef, Q, Sum
from django.urls import reverse
from django.utils import timezone

from . import access_review, invitations, slo
from .gateway import in_flight
from .models import (
    Application,
    ApplicationGrant,
    Authenticator,
    Invitation,
    Organization,
    OrganizationMember,
    User,
)
from .queue_status import lane_status


def locked_usernames():
    """Every user ID axes refuses now, counted as `administration.locked_out` counts."""
    from axes.models import AccessAttempt

    since = timezone.now() - settings.AXES_COOLOFF_TIME
    rows = (
        AccessAttempt.objects.filter(attempt_time__gte=since)
        .exclude(username="")
        .values("username")
        .annotate(failures=Sum("failures_since_start"))
    )
    return sorted(
        row["username"] for row in rows if row["failures"] >= settings.AXES_FAILURE_LIMIT
    )


def unadministered_organizations():
    administered = OrganizationMember.objects.filter(
        organization=OuterRef("pk"), is_admin=True, user__is_active=True
    )
    return Organization.objects.filter(active=True).exclude(Exists(administered)).order_by("name")


def ownerless_applications():
    """Live applications with no active owner who is still in their organization."""
    owned = ApplicationGrant.objects.filter(
        application=OuterRef("pk"),
        role="owner",
        user__is_active=True,
        user__organizationmember__organization=OuterRef("product__portfolio__organization"),
    )
    return (
        Application.objects.filter(active=True)
        .exclude(Exists(owned))
        .select_related("product__portfolio__organization")
        .order_by("name")
    )


def attention(now=None):
    """What needs a platform administrator: (label, count, link, detail) rows, worst first."""
    from .administration import dormant_before

    now = now or timezone.now()
    rows = []

    def add(label, count, link, detail, level="problem"):
        if count:
            rows.append(
                {"label": label, "count": count, "url": link, "detail": detail, "level": level}
            )

    orgs = list(unadministered_organizations())
    add(
        "Organizations with no administrator",
        len(orgs),
        reverse("platform-organizations"),
        ", ".join(org.name for org in orgs[:5]),
    )
    apps = list(ownerless_applications())
    add(
        "Applications nobody owns",
        len(apps),
        reverse("platform-organizations"),
        ", ".join(f"{app.name} ({app.product.portfolio.organization.name})" for app in apps[:5]),
    )
    rows_by_lane = lane_status(now)
    stale = [row["lane"] for row in rows_by_lane if row["stale"]]
    add(
        "Worker lanes with no live process",
        len(stale),
        reverse("platform-operations"),
        ", ".join(stale),
    )
    dead = sum(row["dead"] for row in rows_by_lane)
    add(
        "Background jobs that gave up",
        dead,
        reverse("platform-operations"),
        "Each used every attempt; whoever asked must ask again.",
    )
    missed = [row for row in slo.measure(now) if row["judged"] and not row["met"]]
    add(
        "Service levels below target",
        len(missed),
        reverse("platform-operations"),
        "; ".join(row["promise"] for row in missed),
    )
    due = list(access_review.due_for_review(now).select_related("product__portfolio__organization"))
    add(
        f"Applications whose access is due a review (every {access_review.REVIEW_DAYS} days)",
        len(due),
        reverse("platform-organizations"),
        "Their owners confirm it on the Access screen.",
        level="note",
    )
    locked = locked_usernames()
    add(
        "Accounts locked out",
        len(locked),
        reverse("users") + "?status=locked",
        ", ".join(locked[:5]),
    )
    waiting = Invitation.objects.filter(status=Invitation.Status.AWAITING).count()
    add(
        "Invitations waiting for an organization administrator",
        waiting,
        reverse("platform-organizations"),
        "Their organization's administrators decide them on Members.",
        level="note",
    )
    before = dormant_before()
    dormant = User.objects.filter(is_active=True).filter(
        Q(last_login__lt=before) | Q(last_login__isnull=True, date_joined__lt=before)
    )
    add(
        "Dormant accounts",
        dormant.count(),
        reverse("users") + "?status=dormant",
        "Active, and nobody has signed in to them for a while.",
        level="note",
    )
    return rows


def security(now=None):
    """How people sign in here, for the Sign-in and security page. Settings and counts."""
    active = User.objects.filter(is_active=True)
    admins = active.filter(is_platform_admin=True).order_by("username")
    with_app = set(
        Authenticator.objects.filter(confirmed_at__isnull=False).values_list("user_id", flat=True)
    )
    return {
        "microsoft": bool(settings.ENTRA_TENANT and settings.ENTRA_CLIENT_ID),
        "microsoft_creates": settings.ENTRA_CREATE_USERS,
        "provisioning": settings.SCIM_PROVISIONING,
        "failure_limit": settings.AXES_FAILURE_LIMIT,
        "cooloff_minutes": int(settings.AXES_COOLOFF_TIME.total_seconds() // 60),
        "session_minutes": settings.SESSION_COOKIE_AGE // 60,
        "reset_hours": settings.PASSWORD_RESET_TIMEOUT // 3600,
        "invitation_hours": invitations.LINK_HOURS,
        "people": active.count(),
        "with_authenticator": active.filter(pk__in=with_app).count(),
        "admins": [{"user": admin, "authenticator": admin.pk in with_app} for admin in admins],
        "locked": locked_usernames(),
    }


def operations(now=None):
    now = now or timezone.now()
    return {
        "lanes": lane_status(now),
        "calls": in_flight(),
        "objectives": slo.measure(now),
        "window_days": slo.WINDOW_DAYS,
        "min_events": slo.MIN_EVENTS,
    }


def spend_by_organization():
    """Recorded AI spend per organization and currency, largest first. Receipts only."""
    from .models import AIUsage

    return (
        AIUsage.objects.values("application__product__portfolio__organization__name", "currency")
        .annotate(amount=Sum("amount"), calls=Count("id"))
        .order_by("-amount")
    )
