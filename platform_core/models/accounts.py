"""Who: users, the organization tree, applications, grants and the audit trail."""

import uuid

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.text import slugify


class User(AbstractUser):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    is_platform_admin = models.BooleanField(default=False)
    must_change_password = models.BooleanField(default=False)


def unique_slug(model, name, field="slug"):
    """A readable, unique handle derived from a name.

    Two applications may legitimately share a name in different organizations,
    but one address space cannot, so a collision gets a short suffix rather than
    an error - naming is not the place to make someone retry.
    """
    base = slugify(name)[:120] or "application"
    candidate, index = base, 2
    while model.objects.filter(**{field: candidate}).exists():
        suffix = f"-{index}"
        candidate = f"{base[: 120 - len(suffix)]}{suffix}"
        index += 1
    return candidate


class NamedResource(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True
        ordering = ["name"]

    def __str__(self):
        return self.name


class Organization(NamedResource):
    active = models.BooleanField(default=True)


class OrganizationMember(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="members")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    is_admin = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "user"], name="unique_org_member")
        ]


class Portfolio(NamedResource):
    organization = models.ForeignKey(
        Organization, on_delete=models.PROTECT, related_name="portfolios"
    )


class Product(NamedResource):
    portfolio = models.ForeignKey(Portfolio, on_delete=models.PROTECT, related_name="products")


class Application(NamedResource):
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="applications")
    active = models.BooleanField(default=True)
    #: A readable handle for the API and MCP addresses, so a caller  does not
    #: have to carry a UUID around. Unique across the platform because those
    #: addresses have no organization in the path; guessing one gains nothing,
    #: since every endpoint is bearer-only and a token is bound to one
    #: application.
    slug = models.SlugField(max_length=140, unique=True, blank=True)
    #: When this application first had everything it needs to analyse a ticket.
    #:
    #: Derived state, recorded rather than recomputed. The setup strip appears on
    #: every application screen while onboarding is unfinished, and answering
    #: "is it unfinished?" costs half a dozen queries - which is fine while
    #: somebody is setting up and pure waste for every page view afterwards.
    #: Once earned it is never cleared: unpublishing a graph is a problem for the
    #: run that needs it, and the run says so. It is not a reason to start
    #: greeting somebody who onboarded months ago.
    setup_completed_at = models.DateTimeField(null=True, blank=True)
    identity_icon = models.CharField(max_length=24, blank=True)
    logo_digest = models.CharField(max_length=64, blank=True)

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = unique_slug(Application, self.name)
        super().save(*args, **kwargs)

    @property
    def organization_id(self):
        return self.product.portfolio.organization_id


class ApplicationLogo(models.Model):
    """The optional, sanitized PNG lives separately from application list rows."""

    application = models.OneToOneField(
        Application, on_delete=models.CASCADE, related_name="custom_logo", primary_key=True
    )
    png = models.BinaryField()


class ApplicationGrant(models.Model):
    class Role(models.TextChoices):
        VIEWER = "viewer", "Viewer"
        CONTRIBUTOR = "contributor", "Contributor"
        OWNER = "owner", "Application owner"

    application = models.ForeignKey(Application, on_delete=models.CASCADE, related_name="grants")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    role = models.CharField(max_length=16, choices=Role.choices)
    can_approve = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["application", "user"], name="unique_app_grant"),
            models.CheckConstraint(
                condition=models.Q(role__in=["viewer", "contributor", "owner"]),
                name="valid_app_role",
            ),
        ]


class Branding(models.Model):
    """Small canonical PNG kept in the database for atomic, multi-instance updates."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    png = models.BinaryField()
    digest = models.CharField(max_length=64)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(id=1), name="branding_singleton")]


class AuditEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    organization = models.ForeignKey(Organization, null=True, on_delete=models.PROTECT)
    action = models.CharField(max_length=60)
    details = models.JSONField(default=dict)
    request_id = models.CharField(max_length=36, blank=True)
    resource_id = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class FeatureSwitch(models.Model):
    key = models.CharField(max_length=40, primary_key=True)
    enabled = models.BooleanField(default=True)


class ApplicationFeature(models.Model):
    application = models.ForeignKey(Application, on_delete=models.CASCADE)
    key = models.CharField(max_length=40)
    enabled = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["application", "key"], name="unique_application_feature"
            )
        ]


class ApiToken(models.Model):
    """A machine credential that acts as one user within one application.

    Deliberately not a new kind of principal. The token carries a user and an
    application, and every request it makes runs the same access checks a browser
    session would - so revoking that user's grant, disabling their account or
    turning off a feature switch takes the token with it, and there is no second
    permission model to drift out of step with the first.

    Only a hash of the secret is stored. The value is shown once, at creation.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(Application, on_delete=models.CASCADE, related_name="tokens")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    name = models.CharField(max_length=120)
    #: The public half, used to find the row before verifying the secret.
    prefix = models.CharField(max_length=16, unique=True)
    digest = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["application", "-created_at"])]

    @property
    def active(self):
        from django.utils import timezone as tz

        if self.revoked_at:
            return False
        return not (self.expires_at and self.expires_at <= tz.now())

    @property
    def state(self):
        from django.utils import timezone as tz

        if self.revoked_at:
            return "Revoked"
        if self.expires_at and self.expires_at <= tz.now():
            return "Expired"
        return "Active"


class ManagedCredential(models.Model):
    """Provenance for a credential an owner set from the browser.

    The credential itself is **not** here. It lives in a file with owner-only
    permissions, the same as one an operator mounts, and `secrets.py` is the only
    thing that writes it. This row records who set it and when, plus a digest
    salted with the file name so it can be shown for confirmation without being
    comparable across applications.

    The file is the truth. A row whose file has been removed under it means the
    credential is gone, not that it is still configured - which is why the screen
    reads presence from disk and uses this only for the "set by" line.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(Application, on_delete=models.CASCADE)
    #: The registry key - "jira", "openai" - never the file name, which is
    #: derived from this and the application id where the file is written.
    name = models.CharField(max_length=40)
    digest = models.CharField(max_length=64)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["application", "name"], name="unique_managed_credential"
            )
        ]

    def __str__(self):
        return f"{self.name} for {self.application_id}"


class ExternalIdentity(models.Model):
    """Which account a platform user is in one external system.

    Found by email the first time a permission check needs it, or set by an
    operator (`manage.py link_identity`) where the system hides email
    addresses. One per user, system and site.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    system = models.CharField(max_length=20)
    site = models.CharField(max_length=200)
    #: The system's own id: a Jira Cloud accountId, a Data Center username. Empty
    #: when the lookup found nobody, so the miss is not repeated every sync.
    account = models.CharField(max_length=200, blank=True)
    method = models.CharField(max_length=8, choices=[("email", "By email"), ("manual", "Set")])
    resolved_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "system", "site"], name="unique_identity")
        ]


class SignInIdentity(models.Model):
    """The Microsoft Entra ID account that signs a platform user in.

    Kept apart from `ExternalIdentity` on purpose: that table says who someone
    is in Jira for filtering answers and is written by email lookups, while a
    row here lets its holder sign in. Keyed on the tenant and the account's
    object id (`oid`), which Entra never reuses or lets anyone rename; the
    sign-in name is kept only to show. Once linked, an account is matched by
    `oid` alone, so a name reassigned in the directory later cannot take it.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="sign_in_identities"
    )
    tenant = models.CharField(max_length=36)
    subject = models.CharField(max_length=36)
    name = models.CharField(max_length=254, blank=True)
    method = models.CharField(
        max_length=8,
        choices=[
            ("name", "Matched by sign-in name"),
            ("created", "Created at first sign-in"),
            ("manual", "Linked by an operator"),
        ],
    )
    created_at = models.DateTimeField(auto_now_add=True)
    last_signed_in_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["tenant", "subject"], name="unique_sign_in_subject"),
            models.UniqueConstraint(fields=["user", "tenant"], name="unique_sign_in_user"),
        ]
