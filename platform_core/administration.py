"""Control-plane lifecycle views. These never grant implicit knowledge access."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.views import PasswordChangeView
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST

from .forms import OrganizationAdministratorsForm
from .models import Application, ApplicationGrant, Organization, OrganizationMember, User
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
    return render(
        request,
        "users.html",
        {
            "form": form,
            "accounts": Paginator(User.objects.order_by("username"), 30).get_page(
                request.GET.get("page")
            ),
        },
    )


@login_required
@require_POST
@transaction.atomic
def user_status(request, pk):
    require_platform_admin(request.user)
    user = get_object_or_404(User.objects.select_for_update(), pk=pk)
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
                return redirect("users")
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
                return redirect("users")
    user.is_active = state == "enable"
    user.save(update_fields=["is_active"])
    audit(request.user, "account.enabled" if user.is_active else "account.disabled", user.pk)
    messages.success(request, "Account status updated.")
    return redirect("users")


@login_required
@require_POST
@transaction.atomic
def organization_status(request, pk):
    require_platform_admin(request.user)
    org = get_object_or_404(Organization.objects.select_for_update(), pk=pk)
    state = request.POST.get("state")
    if state not in {"enable", "disable"}:
        from django.http import HttpResponse

        return HttpResponse("Invalid status.", status=400)
    org.active = state == "enable"
    org.save(update_fields=["active"])
    audit(request.user, "organization.enabled" if org.active else "organization.disabled", org.pk)
    messages.success(request, "Organization status updated.")
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
@transaction.atomic
def application_status(request, pk):
    app = get_object_or_404(
        Application.objects.select_for_update().select_related("product__portfolio__organization"),
        pk=pk,
    )
    org = organization_for(request.user, app.organization_id, admin=True)
    state = request.POST.get("state")
    if state not in {"enable", "disable"}:
        from django.http import HttpResponse

        return HttpResponse("Invalid status.", status=400)
    app.active = state == "enable"
    app.save(update_fields=["active"])
    audit(
        request.user, "application.enabled" if app.active else "application.disabled", app.pk, org
    )
    messages.success(request, "Application status updated.")
    return redirect("organization", pk=org.pk)


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
