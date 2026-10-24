"""Access that ends on a date, and owners confirming who still needs it.

**Expiry.** An owner may give a viewer or contributor access until a date -
a contractor, an auditor, a month's cover. `policy.applications_for` stops
reading a grant the moment it passes `expires_at`, so it opens nothing from
then on, on every path (pages, the API, tool calls, the gateway's re-check).
The maintenance lane then revokes it (`expire_due`), audited as any revoke
is, so the Access screen, receipts' authorization and every other reader of
grants agree. An owner's grant never expires: the last owner of an
application must not be able to time out of it.

**Review.** Access is given one grant at a time and nobody looks at the whole
list again, so people keep what a project they left needed. An owner reads
the Access screen and confirms it (`review`); the application records when
and by whom, the platform's Overview counts applications not reviewed within
`REVIEW_DAYS`, and the owner sees the same on the screen. Confirming changes
no grant - removing someone is still a revoke - so a review is a record that
somebody looked, which is what an access audit asks for.
"""

import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import Application, ApplicationGrant, User
from .policy import application_for
from .services import audit

logger = logging.getLogger(__name__)

#: How long a review stands before an application reads as due again.
REVIEW_DAYS = 90
#: Expired grants revoked in one maintenance step.
BATCH = 200
#: The account an expiry's revoke is recorded as, like SCIM's `PROVISIONER`:
#: inactive, with no usable password, so it cannot sign in.
EXPIRY = "access-expiry"


def expiry_account():
    """The non-login account expiries are audited as, or None if a person took the name."""
    user, created = User.objects.get_or_create(
        username=EXPIRY, defaults={"is_active": False, "first_name": "Access expiry"}
    )
    if created:
        user.set_unusable_password()
        user.save(update_fields=["password"])
    if user.is_active or user.is_platform_admin or user.has_usable_password():
        return None
    return user


def review_due_before(now=None):
    return (now or timezone.now()) - timedelta(days=REVIEW_DAYS)


def due_for_review(now=None):
    """Live applications whose access nobody has confirmed within `REVIEW_DAYS`."""
    before = review_due_before(now)
    return Application.objects.filter(active=True).filter(
        Q(access_reviewed_at__lt=before)
        | Q(access_reviewed_at__isnull=True, created_at__lt=before)
    )


def review_state(app, now=None):
    """What the Access screen says about the last review."""
    now = now or timezone.now()
    since = app.access_reviewed_at or app.created_at
    return {
        "reviewed_at": app.access_reviewed_at,
        "reviewed_by": app.access_reviewed_by,
        "due_at": since + timedelta(days=REVIEW_DAYS),
        "due": since < review_due_before(now),
        "days": REVIEW_DAYS,
    }


@transaction.atomic
def review(actor, application_id):
    """An owner confirms everyone listed still needs their access."""
    Application.objects.select_for_update().get(pk=application_id)
    app, _ = application_for(actor, application_id, owner=True)
    grants = ApplicationGrant.objects.filter(application=app).count()
    app.access_reviewed_at = timezone.now()
    app.access_reviewed_by = actor
    app.save(update_fields=["access_reviewed_at", "access_reviewed_by"])
    audit(
        actor,
        "application.access_reviewed",
        app.pk,
        app.product.portfolio.organization,
        details={"application_id": str(app.pk), "grants": grants},
    )
    return grants


def expire_due(now=None):
    """Revoke grants past their expiry, a batch at a time. A maintenance step."""
    now = now or timezone.now()
    expired = list(
        ApplicationGrant.objects.filter(expires_at__lte=now)
        .select_related("application__product__portfolio__organization")
        .order_by("expires_at")[:BATCH]
    )
    actor = expiry_account() if expired else None
    if expired and actor is None:
        # Recording a revoke as whoever holds the name would be a lie in the
        # trail. The grants already open nothing; they wait to be revoked.
        logger.warning("grant expiry paused", extra={"event": "grant_expiry_paused"})
        return False
    for grant in expired:
        with transaction.atomic():
            # Gone already, or changed by an owner since it was read: theirs wins.
            removed, _ = ApplicationGrant.objects.filter(
                pk=grant.pk, expires_at=grant.expires_at
            ).delete()
            if removed:
                audit(
                    actor,
                    "application.access_revoked",
                    grant.user_id,
                    grant.application.product.portfolio.organization,
                    details={
                        "application_id": str(grant.application_id),
                        "role": grant.role,
                        "reason": "expired",
                        "expires_at": grant.expires_at.isoformat(),
                    },
                )
    if expired:
        logger.info("grants expired", extra={"event": "grants_expired", "count": len(expired)})
    return bool(expired)
