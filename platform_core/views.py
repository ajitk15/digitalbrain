import hashlib
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import connection, transaction
from django.db.models import Count, Sum
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from .forms import (
    ApplicationForm,
    BrandingForm,
    GrantForm,
    MemberForm,
    OrganizationForm,
    ResourceForm,
)
from .models import (
    AIUsage,
    Application,
    ApplicationFeature,
    ApplicationGrant,
    AuditEvent,
    Branding,
    FeatureSwitch,
    Organization,
    OrganizationMember,
    Portfolio,
    Product,
    User,
)
from .policy import (
    application_for,
    applications_for,
    organization_for,
    organizations_for,
    require_platform_admin,
)
from .services import FEATURES, audit, change_grant, feature_enabled, update_branding


@require_GET
def live(request):
    return JsonResponse({"status": "ok"})


@require_GET
def ready(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        Organization.objects.exists()
    except Exception:
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ready"})


@require_GET
def logo(request):
    brand = Branding.objects.filter(pk=1).first()
    content = (
        bytes(brand.png)
        if brand
        else (Path(settings.BASE_DIR) / "static/brand/default-logo.png").read_bytes()
    )
    digest = brand.digest if brand else hashlib.sha256(content).hexdigest()
    etag = f'"{digest}"'
    if request.headers.get("If-None-Match") == etag:
        response = HttpResponse(status=304)
    else:
        response = HttpResponse(content, content_type="image/png")
    response["ETag"] = etag
    response["Cache-Control"] = "public, max-age=0, must-revalidate"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required
@require_GET
def dashboard(request):
    return render(
        request,
        "dashboard.html",
        {
            "organizations": organizations_for(request.user),
            "applications": applications_for(request.user),
        },
    )


@login_required
@require_GET
def platform_console(request):
    require_platform_admin(request.user)
    totals = AIUsage.objects.values("currency", "estimated").annotate(
        amount=Sum("amount"), calls=Count("id")
    )
    return render(
        request,
        "platform.html",
        {
            "organizations": Organization.objects.all(),
            "user_count": User.objects.filter(is_active=True).count(),
            "application_count": Application.objects.count(),
            "totals": totals,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def branding(request):
    require_platform_admin(request.user)
    form = BrandingForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        update_branding(request.user, form.cleaned_data["logo"])
        messages.success(request, "Platform logo updated.")
        return redirect("branding")
    return render(request, "branding.html", {"form": form})


@login_required
@require_http_methods(["GET", "POST"])
def create_organization(request):
    require_platform_admin(request.user)
    form = OrganizationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            org = Organization.objects.create(name=form.cleaned_data["name"])
            OrganizationMember.objects.create(
                organization=org, user=form.cleaned_data["administrator"], is_admin=True
            )
            audit(request.user, "organization.created", org.pk)
        messages.success(request, "Organization created with its assigned administrator.")
        return redirect("platform-console")
    return render(
        request,
        "form.html",
        {"form": form, "title": "New organization", "eyebrow": "Platform administration"},
    )


@login_required
@require_GET
def organization(request, pk):
    org = organization_for(request.user, pk)
    admin = OrganizationMember.objects.filter(
        organization=org, user=request.user, is_admin=True
    ).exists()
    return render(
        request,
        "organization.html",
        {
            "organization": org,
            "is_org_admin": admin,
            "portfolios": org.portfolios.prefetch_related("products__applications"),
            "accessible_applications": applications_for(request.user).filter(
                product__portfolio__organization=org
            ),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def organization_members(request, pk):
    org = organization_for(request.user, pk, admin=True)
    form = MemberForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        target = User.objects.filter(username=form.cleaned_data["username"], is_active=True).first()
        if target is None:
            form.add_error("username", "This user ID is not available for membership.")
        else:
            with transaction.atomic():
                _, created = OrganizationMember.objects.get_or_create(organization=org, user=target)
                if created:
                    audit(request.user, "organization.member_added", target.pk, org)
            messages.success(
                request, "Membership saved. Application access is assigned separately."
            )
            return redirect("organization-members", pk=pk)
    return render(
        request,
        "members.html",
        {"form": form, "organization": org, "members": org.members.select_related("user")},
    )


@login_required
@require_http_methods(["GET", "POST"])
def create_portfolio(request, pk):
    org = organization_for(request.user, pk, admin=True)
    form = ResourceForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            portfolio = Portfolio.objects.create(organization=org, **form.cleaned_data)
            audit(request.user, "portfolio.created", portfolio.pk, org)
        return redirect("organization", pk=org.pk)
    return render(
        request, "form.html", {"form": form, "title": "New portfolio", "eyebrow": org.name}
    )


@login_required
@require_http_methods(["GET", "POST"])
def create_product(request, pk):
    portfolio = get_object_or_404(Portfolio, pk=pk)
    org = organization_for(request.user, portfolio.organization_id, admin=True)
    form = ResourceForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            product = Product.objects.create(portfolio=portfolio, **form.cleaned_data)
            audit(request.user, "product.created", product.pk, org)
        return redirect("organization", pk=org.pk)
    return render(
        request, "form.html", {"form": form, "title": "New product", "eyebrow": portfolio.name}
    )


@login_required
@require_http_methods(["GET", "POST"])
def create_application(request, pk):
    product = get_object_or_404(Product.objects.select_related("portfolio"), pk=pk)
    org = organization_for(request.user, product.portfolio.organization_id, admin=True)
    form = ApplicationForm(request.POST or None, organization=org)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            app = Application.objects.create(product=product, name=form.cleaned_data["name"])
            ApplicationGrant.objects.create(
                application=app,
                user=form.cleaned_data["owner"],
                role="owner",
                can_approve=form.cleaned_data["owner_can_approve"],
            )
            audit(request.user, "application.created", app.pk, org)
        messages.success(request, "Application created with explicit owner access.")
        return redirect("organization", pk=org.pk)
    return render(
        request, "form.html", {"form": form, "title": "New application", "eyebrow": product.name}
    )


@login_required
@require_http_methods(["GET", "POST"])
def application_access(request, pk):
    app, grant = application_for(request.user, pk, owner=True)
    form = GrantForm(request.POST or None, application=app)
    if request.method == "POST" and form.is_valid():
        if request.POST.get("action") not in {"save", "revoke"}:
            form.add_error(None, "Choose a valid action.")
        else:
            try:
                change_grant(
                    request.user,
                    pk,
                    form.cleaned_data["user"],
                    form.cleaned_data["role"],
                    form.cleaned_data["can_approve"],
                    revoke=request.POST["action"] == "revoke",
                )
            except ValidationError as error:
                form.add_error(None, error)
            else:
                messages.success(request, "Application access updated.")
                return redirect("dashboard")
    return render(
        request,
        "access.html",
        {
            "application": app,
            "form": form,
            "grants": app.grants.select_related("user"),
            "grant": grant,
        },
    )


@login_required
@require_GET
def audit_log(request):
    if request.user.is_platform_admin:
        # Platform admins see control-plane events only; no application event stream.
        events = AuditEvent.objects.filter(organization__isnull=True)
    else:
        org_ids = OrganizationMember.objects.filter(
            user=request.user, is_admin=True, organization__active=True
        ).values("organization_id")
        events = AuditEvent.objects.filter(organization_id__in=org_ids)
    return render(
        request,
        "audit.html",
        {"page": Paginator(events.select_related("actor"), 30).get_page(request.GET.get("page"))},
    )


@login_required
@require_GET
def usage(request, pk):
    app, grant = application_for(request.user, pk)
    if not feature_enabled("usage_reports", app):
        raise Http404
    records = AIUsage.objects.filter(application=app)
    totals = records.values("currency", "estimated").annotate(
        amount=Sum("amount"),
        calls=Count("id"),
        input=Sum("input_tokens"),
        output=Sum("output_tokens"),
    )
    return render(
        request,
        "usage.html",
        {
            "application": app,
            "totals": totals,
            "page": Paginator(records, 30).get_page(request.GET.get("page")),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def features(request, pk=None):
    if pk is None:
        require_platform_admin(request.user)
        app = None
    else:
        app, _ = application_for(request.user, pk, owner=True)
    if request.method == "POST":
        key = request.POST.get("feature")
        state = request.POST.get("state")
        if key not in FEATURES or not FEATURES[key][1] or state not in {"on", "off"}:
            return HttpResponse("Invalid or unavailable feature.", status=400)
        with transaction.atomic():
            if app:
                ApplicationFeature.objects.update_or_create(
                    application=app, key=key, defaults={"enabled": state == "on"}
                )
            else:
                FeatureSwitch.objects.update_or_create(key=key, defaults={"enabled": state == "on"})
            audit(
                request.user,
                "feature.enabled" if state == "on" else "feature.disabled",
                key,
                app.product.portfolio.organization if app else None,
            )
        messages.success(request, "Feature setting updated.")
        return redirect("application-features", pk=pk) if app else redirect("features")
    rows = []
    for key, (label, available) in FEATURES.items():
        model = ApplicationFeature.objects.filter(application=app) if app else FeatureSwitch.objects
        entry = model.filter(key=key).first()
        configured = entry.enabled if entry else True
        rows.append(
            {
                "key": key,
                "label": label,
                "available": available,
                "enabled": available and configured,
                "effective": feature_enabled(key, app) if app else available and configured,
            }
        )
    return render(request, "features.html", {"features": rows, "application": app})


@login_required
@require_http_methods(["GET", "POST"])
def api_tokens(request, pk):
    """Issue and revoke API tokens for one application.

    A token acts as the user who created it, so anyone with application access may
    hold one, and it can never reach further than they can.
    """
    from datetime import timedelta

    from .api_auth import issue
    from .models import ApiToken
    from .workbench import access

    app, grant = access(request.user, pk, "knowledge")
    created = None
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "create":
            name = request.POST.get("name", "").strip()[:120]
            if not name:
                messages.error(request, "Name the token so you can recognise it later.")
            else:
                days = request.POST.get("expires", "90")
                expires = None
                if days.isdigit() and int(days) > 0:
                    expires = timezone.now() + timedelta(days=min(int(days), 365))
                prefix, secret, digest = issue()
                ApiToken.objects.create(
                    application=app,
                    user=request.user,
                    name=name,
                    prefix=prefix,
                    digest=digest,
                    expires_at=expires,
                )
                audit(
                    request.user,
                    "api_token.created",
                    app.pk,
                    app.product.portfolio.organization,
                    details={"name": name, "prefix": prefix},
                )
                # Shown once. Only the digest was stored.
                created = secret
        elif action == "revoke":
            token = get_object_or_404(
                ApiToken, pk=request.POST.get("token"), application=app, user=request.user
            )
            ApiToken.objects.filter(pk=token.pk, revoked_at__isnull=True).update(
                revoked_at=timezone.now()
            )
            audit(
                request.user,
                "api_token.revoked",
                app.pk,
                app.product.portfolio.organization,
                details={"name": token.name, "prefix": token.prefix},
            )
            messages.success(request, f"Token {token.name} revoked.")
            return redirect("api-tokens", pk=pk)
        else:
            raise Http404
    return render(
        request,
        "api_tokens.html",
        {
            "application": app,
            "grant": grant,
            "tokens": ApiToken.objects.filter(application=app, user=request.user),
            "created": created,
        },
    )
