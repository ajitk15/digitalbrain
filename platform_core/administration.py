"""Control-plane lifecycle views. These never grant implicit knowledge access."""

from datetime import timedelta
from urllib.parse import urlencode

from axes.utils import reset as axes_reset
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.hashers import UNUSABLE_PASSWORD_PREFIX
from django.contrib.auth.tokens import default_token_generator
from django.contrib.auth.views import PasswordChangeView, PasswordResetConfirmView
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import authenticator, hierarchy, offboarding
from .api_auth import revoke_all
from .forms import OrganizationAdministratorsForm
from .models import (
    ApiToken,
    Application,
    ApplicationGrant,
    Organization,
    OrganizationMember,
    ProvisionedUser,
    User,
)
from .policy import organization_for, require_platform_admin
from .services import audit


class ProvisionUserForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "first_name", "last_name", "email")


class RequiredPasswordChangeView(PasswordChangeView):
    template_name = "registration/password.html"
    success_url = "/"

    def form_valid(self, form):
        with transaction.atomic():
            response = super().form_valid(form)
            self.request.user.must_change_password = False
            self.request.user.save(update_fields=["must_change_password"])
            audit(self.request.user, "account.password_changed", self.request.user.pk)
        return response


class ResetPasswordView(PasswordResetConfirmView):
    """Where a reset link a platform administrator issued lands.

    Django's own confirm view: the token is bound to the account's current
    password hash and last sign-in, so it works once, and expires after
    PASSWORD_RESET_TIMEOUT. No email is involved - the platform has no mail
    server - so the administrator hands the link over, as they used to hand
    over a first password, except that they never learn the new one.
    """

    template_name = "registration/reset.html"
    success_url = reverse_lazy("login")

    def form_valid(self, form):
        with transaction.atomic():
            response = super().form_valid(form)
            user = form.user
            user.must_change_password = False
            user.save(update_fields=["must_change_password"])
            audit(user, "account.password_reset", user.pk)
        axes_reset(username=user.get_username())
        messages.success(self.request, "Password set. Sign in with it now.")
        return response


#: Never signed in for this long - or never at all, and created that long ago -
#: and an account reads as dormant: still able to sign in, nobody using it.
DORMANT_DAYS = 90

SIGN_IN = {
    "password": "Password",
    "microsoft": "Microsoft",
    "directory": "Directory",
}
STATUS = {
    "active": "Active",
    "disabled": "Disabled",
    "locked": "Locked out",
    "dormant": "Dormant",
}


def dormant_before():
    return timezone.now() - timedelta(days=DORMANT_DAYS)


def is_dormant(user):
    seen = user.last_login or user.date_joined
    return user.is_active and seen < dormant_before()


def locked_out(usernames):
    """Which of these user IDs axes refuses now, counted as axes counts them.

    The failures of attempts still inside the cool-off, summed per user ID. A
    lockout counted against an address rather than a name is not anyone's, and
    clears itself.
    """
    from axes.models import AccessAttempt

    since = timezone.now() - settings.AXES_COOLOFF_TIME
    rows = (
        AccessAttempt.objects.filter(username__in=list(usernames), attempt_time__gte=since)
        .values("username")
        .annotate(failures=Sum("failures_since_start"))
    )
    return {row["username"] for row in rows if row["failures"] >= settings.AXES_FAILURE_LIMIT}


def sign_in_kinds(user):
    """How this account can sign in or is managed, for the People screens."""
    kinds = []
    if user.has_usable_password():
        kinds.append(SIGN_IN["password"])
    if user.sign_in_identities.all():
        kinds.append(SIGN_IN["microsoft"])
    if ProvisionedUser.objects.filter(user=user).exists():
        kinds.append(SIGN_IN["directory"])
    return kinds


def filtered_users(request):
    """The Users list, narrowed by what the search form asked for."""
    found = User.objects.annotate(
        organizations=Count("organizationmember", distinct=True)
    ).prefetch_related("sign_in_identities")
    query = request.GET.get("q", "").strip()[:150]
    if query:
        found = found.filter(
            Q(username__icontains=query)
            | Q(first_name__icontains=query)
            | Q(last_name__icontains=query)
            | Q(email__icontains=query)
        )
    sign_in = request.GET.get("sign_in", "")
    if sign_in == "password":
        found = found.exclude(password__startswith=UNUSABLE_PASSWORD_PREFIX).exclude(password="")
    elif sign_in == "microsoft":
        found = found.filter(sign_in_identities__isnull=False).distinct()
    elif sign_in == "directory":
        found = found.filter(provisioning__isnull=False)
    status = request.GET.get("status", "")
    if status == "active":
        found = found.filter(is_active=True)
    elif status == "disabled":
        found = found.filter(is_active=False)
    elif status == "dormant":
        before = dormant_before()
        found = found.filter(is_active=True).filter(
            Q(last_login__lt=before) | Q(last_login__isnull=True, date_joined__lt=before)
        )
    elif status == "locked":
        found = found.filter(username__in=locked_out(found.values_list("username", flat=True)))
    return found.order_by("username"), {"q": query, "sign_in": sign_in, "status": status}


@login_required
@require_http_methods(["GET", "POST"])
def users(request):
    require_platform_admin(request.user)
    form = ProvisionUserForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            user = form.save(commit=False)
            user.must_change_password = True
            user.save()
            audit(request.user, "account.created", user.pk)
        messages.success(request, "User created. They must change their password at first sign-in.")
        return redirect("users")
    found, chosen = filtered_users(request)
    page = Paginator(found, 30).get_page(request.GET.get("page"))
    locked = locked_out(account.username for account in page)
    for account in page:
        account.sign_in = sign_in_kinds(account)
        account.locked = account.username in locked
        account.dormant = is_dormant(account)
    return render(
        request,
        "users.html",
        {
            "form": form,
            "accounts": page,
            "chosen": chosen,
            "sign_in_choices": SIGN_IN,
            "status_choices": STATUS,
            "extra_query": urlencode({key: value for key, value in chosen.items() if value}),
        },
    )


@login_required
@require_GET
def person(request, pk):
    """Everything platform administration knows about one account.

    Names and roles only: the organizations, applications and tokens listed
    here are not links, because administering an account is not access to what
    it can open (`policy.py`).
    """
    require_platform_admin(request.user)
    account = get_object_or_404(User, pk=pk)
    return render(request, "person.html", person_context(account))


def person_context(account, **extra):
    grants = (
        ApplicationGrant.objects.filter(user=account)
        .select_related("application__product__portfolio__organization")
        .order_by("application__name")
    )
    return {
        "account": account,
        "sign_in": sign_in_kinds(account),
        "locked": account.username in locked_out([account.username]),
        "dormant": is_dormant(account),
        "dormant_days": DORMANT_DAYS,
        "memberships": OrganizationMember.objects.filter(user=account)
        .select_related("organization")
        .order_by("organization__name"),
        "grants": grants,
        "tokens": ApiToken.objects.filter(user=account, revoked_at__isnull=True)
        .select_related("application")
        .order_by("application__name", "name"),
        "identities": account.sign_in_identities.order_by("created_at"),
        "resettable": account.is_active and not account.is_platform_admin,
        "authenticator": authenticator.enrolled(account),
        **extra,
    }


@login_required
@require_POST
def person_unlock(request, pk):
    """Clear the failed sign-ins counted against one user ID."""
    require_platform_admin(request.user)
    account = get_object_or_404(User, pk=pk)
    cleared = axes_reset(username=account.username)
    audit(request.user, "account.unlocked", account.pk, details={"attempts": cleared})
    messages.success(request, f"{account.username} can sign in again.")
    return redirect("person", pk=pk)


@login_required
@require_POST
def person_reset_link(request, pk):
    """A one-time link that lets this person choose a new password.

    Shown once, in this response, and never stored. Refused for a platform
    administrator - another platform administrator taking over that account
    would be the one thing it changes - who resets on the host instead
    (`manage.py changepassword`), and for a disabled account.
    """
    require_platform_admin(request.user)
    account = get_object_or_404(User, pk=pk)
    if account.is_platform_admin or not account.is_active:
        raise PermissionDenied("Reset links are not issued for this account.")
    uid = urlsafe_base64_encode(force_bytes(account.pk))
    path = reverse("password-reset", args=[uid, default_token_generator.make_token(account)])
    audit(request.user, "account.reset_link_issued", account.pk)
    hours = settings.PASSWORD_RESET_TIMEOUT // 3600
    return render(
        request,
        "person.html",
        person_context(account, reset_link=request.build_absolute_uri(path), reset_hours=hours),
    )


@login_required
@require_http_methods(["GET", "POST"])
def person_offboard(request, pk):
    """Name a successor, hand them what this person held, and disable the account."""
    require_platform_admin(request.user)
    account = get_object_or_404(User, pk=pk, is_active=True)
    if account.is_platform_admin:
        raise PermissionDenied("A platform administrator is offboarded on the host.")
    error = ""
    if request.method == "POST":
        try:
            done = offboarding.offboard(request.user, account.pk, request.POST.get("successor", ""))
        except ValidationError as refusal:
            error = " ".join(refusal.messages)
        else:
            messages.success(
                request,
                f"{account.username} is offboarded. {done['successor'].username} now owns "
                f"{done['applications']} application{'' if done['applications'] == 1 else 's'}"
                f" and administers {done['organizations']} organization"
                f"{'' if done['organizations'] == 1 else 's'}.",
            )
            return redirect("person", pk=pk)
    return render(
        request,
        "offboard.html",
        {
            "account": account,
            "error": error,
            "successor": request.POST.get("successor", ""),
            **offboarding.holdings(account),
        },
    )


@login_required
@require_POST
def person_authenticator(request, pk):
    """Remove someone's authenticator app, for a lost phone and no recovery code.

    Refused for a platform administrator, as a reset link is: the host does
    that (`manage.py remove_authenticator`).
    """
    require_platform_admin(request.user)
    account = get_object_or_404(User, pk=pk)
    if account.is_platform_admin:
        raise PermissionDenied("A platform administrator's app is removed on the host.")
    if authenticator.remove(account, request.user, "removed by a platform administrator"):
        messages.success(request, f"{account.username} signs in with their password alone now.")
    return redirect("person", pk=pk)


@login_required
@require_POST
@transaction.atomic
def user_status(request, pk):
    require_platform_admin(request.user)
    user = get_object_or_404(User.objects.select_for_update(), pk=pk)
    back = redirect("person", pk=pk) if request.POST.get("next") == "person" else redirect("users")
    state = request.POST.get("state")
    if user.is_platform_admin or state not in {"enable", "disable"}:
        raise PermissionDenied("Platform administrators cannot be disabled from this screen.")
    if state == "disable":
        org_ids = OrganizationMember.objects.filter(user=user, is_admin=True).values(
            "organization_id"
        )
        for org in Organization.objects.select_for_update().filter(pk__in=org_ids):
            if (
                not OrganizationMember.objects.filter(
                    organization=org, is_admin=True, user__is_active=True
                )
                .exclude(user=user)
                .exists()
            ):
                messages.error(
                    request, "Assign another organization administrator before disabling this user."
                )
                return back
        owned_ids = ApplicationGrant.objects.filter(user=user, role="owner").values(
            "application_id"
        )
        for app in Application.objects.select_for_update().filter(pk__in=owned_ids):
            if (
                not ApplicationGrant.objects.filter(
                    application=app,
                    role="owner",
                    user__is_active=True,
                    user__organizationmember__organization_id=app.organization_id,
                )
                .exclude(user=user)
                .exists()
            ):
                messages.error(
                    request, "Assign another active application owner before disabling this user."
                )
                return back
    user.is_active = state == "enable"
    user.save(update_fields=["is_active"])
    audit(request.user, "account.enabled" if user.is_active else "account.disabled", user.pk)
    revoked = 0 if user.is_active else revoke_all(user, request.user, reason="account disabled")
    messages.success(
        request,
        "Account status updated."
        + (f" {revoked} API token{'' if revoked == 1 else 's'} revoked." if revoked else ""),
    )
    return back


def confirm_disable(request, obj, action, cancel_url):
    """What a disable turns off and stops, asked before it is done.

    Disabling was one click on the console and on Structure, though it fails
    every job still queued under the level. Enabling restores exactly what was
    on and stops nothing, so it is still one click.
    """
    ids = list(hierarchy.applications_under(obj).values_list("pk", flat=True))
    return render(
        request,
        "disable_confirm.html",
        {
            "item": obj,
            "level": hierarchy.level_of(obj),
            "applications": len(ids),
            "waiting": hierarchy.waiting(ids),
            "action": action,
            "cancel_url": cancel_url,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def organization_status(request, pk):
    require_platform_admin(request.user)
    org = get_object_or_404(Organization, pk=pk)
    if request.method == "GET":
        return confirm_disable(
            request, org, reverse("organization-status", args=[pk]), reverse("platform-console")
        )
    state = request.POST.get("state")
    if state not in {"enable", "disable"}:
        return HttpResponse("Invalid status.", status=400)
    stopped = hierarchy.set_active(request.user, org, state == "enable")
    messages.success(request, "Organization status updated." + stopped_note(stopped))
    return redirect("platform-console")


@login_required
@require_http_methods(["GET", "POST"])
def organization_administrators(request, pk):
    """Promote and demote an organization's administrators after creation.

    A platform administrator manages this without being a member: the platform
    console already lists every organization, and nothing here grants the
    platform administrator membership or application access. Demoting keeps the
    membership - removing someone from the organization is a different act.
    """
    require_platform_admin(request.user)
    org = get_object_or_404(Organization, pk=pk)
    current = set(
        OrganizationMember.objects.filter(organization=org, is_admin=True).values_list(
            "user_id", flat=True
        )
    )
    form = OrganizationAdministratorsForm(
        request.POST or None, organization=org, initial={"administrators": list(current)}
    )
    if request.method == "POST" and form.is_valid():
        chosen = {user.pk for user in form.cleaned_data["administrators"]}
        with transaction.atomic():
            Organization.objects.select_for_update().get(pk=org.pk)
            current = set(
                OrganizationMember.objects.filter(organization=org, is_admin=True).values_list(
                    "user_id", flat=True
                )
            )
            for user_id in chosen - current:
                OrganizationMember.objects.update_or_create(
                    organization=org, user_id=user_id, defaults={"is_admin": True}
                )
                audit(request.user, "organization.admin_added", user_id, org)
            removed = current - chosen
            if removed:
                OrganizationMember.objects.filter(organization=org, user_id__in=removed).update(
                    is_admin=False
                )
                for user_id in removed:
                    audit(request.user, "organization.admin_removed", user_id, org)
        messages.success(request, f"Administrators of {org.name} updated.")
        return redirect("platform-console")
    return render(
        request,
        "organization_admins.html",
        {"form": form, "organization": org, "cancel_url": reverse("platform-console")},
    )


@login_required
@require_POST
def application_status(request, pk):
    app = get_object_or_404(
        Application.objects.select_related("product__portfolio__organization"), pk=pk
    )
    org = organization_for(request.user, app.organization_id, admin=True)
    state = request.POST.get("state")
    if state not in {"enable", "disable"}:
        return HttpResponse("Invalid status.", status=400)
    stopped = hierarchy.set_active(request.user, app, state == "enable")
    messages.success(request, "Application status updated." + stopped_note(stopped))
    return redirect("organization", pk=org.pk)


def stopped_note(stopped):
    if not stopped:
        return ""
    listed = ", ".join(f"{count} {label}" for label, count in stopped.items())
    return f" Stopped before they started: {listed}."


# ── the organization tree, for a platform administrator ──────────────────────


def platform_admin_or_404(user):
    """Everyone else is told nothing exists here, as an application does (`policy`)."""
    try:
        require_platform_admin(user)
    except PermissionDenied:
        raise Http404 from None


#: What each level's page needs from above it, read in the same query.
PARENTS = {
    "portfolio": ("organization",),
    "product": ("portfolio__organization",),
    "application": ("product__portfolio__organization",),
}


def level_or_404(level, pk):
    if level not in hierarchy.LEVELS:
        raise Http404
    model, _ = hierarchy.LEVELS[level]
    return get_object_or_404(model.objects.select_related(*PARENTS.get(level, ())), pk=pk)


@login_required
@require_GET
def organization_structure(request, pk):
    """Disable, enable and delete each level of one organization.

    Names, states and counts only. A platform administrator is usually not a
    member of the organization and holds no grant, so no application is
    linked: administering the tree is not reading what is in it.
    """
    platform_admin_or_404(request.user)
    org = get_object_or_404(Organization, pk=pk)
    return render(
        request,
        "organization_structure.html",
        {
            "organization": org,
            "portfolios": org.portfolios.prefetch_related("products__applications").order_by(
                "name", "pk"
            ),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def level_status(request, level, pk):
    platform_admin_or_404(request.user)
    obj = level_or_404(level, pk)
    if request.method == "GET":
        return confirm_disable(
            request,
            obj,
            reverse("level-status", args=[level, pk]),
            reverse("organization-structure", args=[hierarchy.organization_of(obj).pk]),
        )
    state = request.POST.get("state")
    if state not in {"enable", "disable"}:
        return HttpResponse("Invalid status.", status=400)
    stopped = hierarchy.set_active(request.user, obj, state == "enable")
    done = "enabled" if state == "enable" else "disabled"
    messages.success(request, f"{obj.name} {done}." + stopped_note(stopped))
    return redirect("organization-structure", pk=hierarchy.organization_of(obj).pk)


@login_required
@require_POST
def level_delete(request, level, pk):
    """Delete one level and everything under it. There is no undo.

    The name is typed rather than confirmed, as a demonstration reset's is, and
    the level must already be disabled: deleting is the second of two steps.
    """
    platform_admin_or_404(request.user)
    obj = level_or_404(level, pk)
    organization = hierarchy.organization_of(obj)
    try:
        result = hierarchy.delete(request.user, obj, request.POST.get("confirm"))
    except ValidationError as error:
        messages.error(request, " ".join(error.messages))
        return redirect("organization-structure", pk=organization.pk)
    count = result["removed"].get("Application", 0)
    note = f"{obj.name} was deleted with {count} application{'' if count == 1 else 's'}."
    if result["files_left"]:
        note += (
            f" {result['files_left']} original file(s) could not be removed because the "
            "document store did not answer; the audit log records it."
        )
    messages.success(request, note)
    if level == "organization":
        return redirect("platform-console")
    return redirect("organization-structure", pk=organization.pk)


@login_required
@require_POST
def reset_organization(request, pk):
    """Clear one organization's graphs and runs between demonstrations.

    POST only and nothing to GET: there is no page for this, only a panel on the
    platform console that already shows what a reset would remove. A screen of
    its own would be somewhere to arrive by accident.
    """
    from . import demo_reset

    require_platform_admin(request.user)
    organization = get_object_or_404(Organization, pk=pk)
    try:
        removed = demo_reset.reset(request.user, organization, request.POST.get("confirm"))
    except ValidationError as error:
        messages.error(request, " ".join(error.messages))
        return redirect("platform-console")
    runs = removed.get("FactoryRun", 0)
    repositories = removed.get("CodeRepository", 0)
    versions = removed.get("GraphRevision", 0)
    imports = removed.get("KnowledgeEntry imported", 0)
    sources = removed.get("KnowledgeSource", 0)
    documents = removed.get("Document from sources", 0)
    messages.success(
        request,
        f"{organization.name} was reset: {runs} run(s), {repositories} code "
        f"repositor{'y' if repositories == 1 else 'ies'}, {versions} graph "
        f"version(s), {imports} imported record(s) and {sources} link source(s) "
        f"with {documents} document(s) removed. Applications, connectors, "
        "credentials, uploaded documents, settings and people are untouched.",
    )
    return redirect("platform-console")
