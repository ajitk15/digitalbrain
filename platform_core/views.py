import hashlib
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import connection, transaction
from django.db.models import Count, Prefetch, Q, Sum
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from . import access_review, console, invitations, sorting
from .ai import seed_application_ai
from .forms import (
    AcceptInvitationForm,
    ApplicationForm,
    ApplicationIdentityForm,
    BrandingForm,
    ChatRetentionForm,
    GrantForm,
    InviteForm,
    MemberForm,
    OrganizationForm,
    ResourceForm,
)
from .models import (
    AIUsage,
    Application,
    ApplicationFeature,
    ApplicationGrant,
    ApplicationLogo,
    AuditEvent,
    Branding,
    ChatConversation,
    ChatRetention,
    FeatureSwitch,
    Invitation,
    Organization,
    OrganizationMember,
    Portfolio,
    Product,
    User,
)
from .policy import (
    CAPABILITIES,
    application_for,
    applications_for,
    organization_for,
    organizations_for,
    require_platform_admin,
)
from .services import (
    AREA_ICONS,
    AREA_LABELS,
    FEATURES,
    area_of,
    audit,
    change_grant,
    control_plane,
    feature_enabled,
    knowledge_labels,
    purposes,
    remove_member,
    update_branding,
)


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


@lru_cache(maxsize=1)
def default_logo():
    """The shipped logo and its digest, read once.

    Every page asks for this, the response must revalidate, and an unbranded
    deployment has no Branding row - so without the cache each request re-read a
    third of a megabyte off disk and hashed it again. A missing file is answered
    with an empty body rather than a 500: this endpoint is on the sign-in page, and
    a deployment that lost one static file should still let people sign in.
    """
    try:
        content = (Path(settings.BASE_DIR) / "static/brand/default-logo.png").read_bytes()
    except OSError:
        content = b""
    return content, hashlib.sha256(content).hexdigest()


@require_GET
def favicon(request):
    """/favicon.ico, which browsers ask for whether or not a page names an icon.

    Every page links the icon through its hashed static URL; this answers the
    bare path those requests use, by sending them to the same file rather than
    letting them 404 into the error log. Resolved per request, so it always
    names the current manifest entry.
    """
    from django.templatetags.static import static

    return redirect(static("brand/favicon.ico"), permanent=False)


def logo(request):
    brand = Branding.objects.filter(pk=1).first()
    content, digest = (bytes(brand.png), brand.digest) if brand else default_logo()
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
def application_logo(request, pk):
    app, _ = application_for(request.user, pk)
    if not app.logo_digest:
        raise Http404
    image = get_object_or_404(ApplicationLogo, application=app)
    etag = f'"{app.logo_digest}"'
    response = (
        HttpResponse(status=304)
        if request.headers.get("If-None-Match") == etag
        else HttpResponse(bytes(image.png), content_type="image/png")
    )
    response["ETag"] = etag
    response["Cache-Control"] = "private, max-age=0, must-revalidate"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required
@require_http_methods(["GET", "POST"])
def application_identity(request, pk):
    """Optional appearance, editable by an application owner at any time."""
    app, _ = application_for(request.user, pk, owner=True)
    current_style = "logo" if app.logo_digest else "icon" if app.identity_icon else "letter"
    form = ApplicationIdentityForm(
        request.POST or None,
        request.FILES or None,
        has_logo=bool(app.logo_digest),
        initial={"style": current_style, "icon": app.identity_icon or "code"},
    )
    if request.method == "POST" and form.is_valid():
        style = form.cleaned_data["style"]
        png = form.cleaned_data["logo"]
        with transaction.atomic():
            if style == "logo" and png:
                ApplicationLogo.objects.update_or_create(
                    application=app, defaults={"png": png}
                )
                app.logo_digest = hashlib.sha256(png).hexdigest()
            elif style != "logo":
                ApplicationLogo.objects.filter(application=app).delete()
                app.logo_digest = ""
            app.identity_icon = form.cleaned_data["icon"] if style == "icon" else ""
            app.save(update_fields=["identity_icon", "logo_digest"])
            audit(
                request.user,
                "application.identity_updated",
                app.pk,
                app.product.portfolio.organization,
            )
        messages.success(request, "Application appearance updated.")
        destination = (
            "onboarding"
            if request.POST.get("return_to") == "onboarding"
            else "application-identity"
        )
        if destination == "onboarding":
            return redirect(destination, pk=app.pk)
        return redirect(destination, pk=app.pk)
    return render(
        request,
        "application_identity.html",
        {"application": app, "form": form, "return_to": request.GET.get("return_to")},
    )


@login_required
@require_http_methods(["GET", "POST"])
def chat_settings(request, pk):
    """Chat history retention for one application. Owner-only, like Features."""
    app, grant = application_for(request.user, pk, owner=True)
    if not feature_enabled("chat", app):
        # 403, matching workbench.access. The 404-not-403 rule in CLAUDE.md is
        # about a *missing grant*, where confirming the application exists is
        # itself a disclosure. This caller already holds an owner grant, so
        # pretending the page is absent tells them nothing they do not know and
        # hides the one fact that would help: the feature is switched off.
        raise PermissionDenied("This feature is disabled.")
    record, _ = ChatRetention.objects.get_or_create(application=app)
    form = ChatRetentionForm(request.POST or None, instance=record)
    if request.method == "POST":
        if form.is_valid():
            saved = form.save(commit=False)
            saved.updated_by = request.user
            saved.save()
            audit(
                request.user,
                "chat.retention_changed",
                app.pk,
                app.product.portfolio.organization,
                details={"days": saved.days},
            )
            messages.success(
                request,
                "Retention updated. Conversations are kept indefinitely."
                if saved.days == 0
                else f"Retention updated to {saved.days} day(s).",
            )
            return redirect("chat-settings", pk=pk)
    return render(
        request,
        "chat_settings.html",
        {
            "application": app,
            "grant": grant,
            "form": form,
            "conversation_count": ChatConversation.objects.filter(application=app).count(),
        },
    )


def server_error(request):
    """The 500 page, carrying the request id the operator will need.

    Django's default handler renders with an empty context, so the page could
    only mention that an id exists without ever showing it.

    Deliberately undecorated. Django calls this for whatever method was in
    flight, so a `require_GET` here answered every failed POST in the platform
    with an empty 405 - the browser showing its own "this page isn't working"
    instead of the page that carries the request id.
    """
    from django.shortcuts import render as _render

    return _render(
        request, "500.html", {"request_id": getattr(request, "request_id", "")}, status=500
    )


@login_required
@require_GET
def application_home(request, pk):
    """An application opens on Knowledge; documents are one rail inside it.

    This stays a redirect rather than a second route onto the graph view so that
    Knowledge keeps one canonical URL. `application_for` runs first, so a user
    with no grant still gets 404 here and learns nothing about the application -
    the redirect must never become a way to probe for existence.
    """
    app, _ = application_for(request.user, pk)
    if feature_enabled("knowledge", app):
        return redirect("graph", pk=pk)
    # With Knowledge switched off there is no graph to show, and the document
    # list is the only thing the application still has.
    return redirect("documents", pk=pk)


@login_required
@require_GET
def attention_list(request):
    """Everything the Overview's attention list holds, filterable.

    The Overview shows the most pressing few and used to say the rest were "on
    each application's own screens" - true, and no help finding them. Filters
    narrow the caller's own items and nothing else: `attention.items` is scoped
    by their grants, so a foreign application id matches nothing.
    """
    from . import attention

    applications = list(applications_for(request.user))
    everything = attention.items(request.user, applications)
    application = request.GET.get("application", "")
    kind = request.GET.get("kind", "")
    shown = [
        item for item in everything if not application or str(item.application.pk) == application
    ]
    kinds = [
        (key, label, sum(1 for item in shown if item.kind == key))
        for key, label in attention.KINDS.items()
    ]
    in_application = len(shown)
    if kind in attention.KINDS:
        shown = [item for item in shown if item.kind == kind]
    else:
        kind = ""
    present = {item.application.pk for item in everything}
    return render(
        request,
        "attention.html",
        {
            "everything": everything,
            "applications": [app for app in applications if app.pk in present],
            # Not "application": base.html reads that name as the current
            # application object, for the sidebar.
            "chosen": application,
            "kind": kind,
            "kinds": [entry for entry in kinds if entry[2]],
            "in_application": in_application,
            "page": Paginator(shown, 50).get_page(request.GET.get("page")),
        },
    )


@login_required
@require_GET
def dashboard(request):
    """Where to start and what is waiting, rather than how many things exist."""
    from . import attention

    applications = list(applications_for(request.user))
    waiting = attention.items(request.user, applications)
    return render(
        request,
        "dashboard.html",
        {
            "organizations": organizations_for(request.user),
            "applications": applications,
            "cards": attention.cards(applications),
            "attention": waiting[: attention.MAX_ITEMS],
            "attention_total": len(waiting),
        },
    )


@login_required
@require_GET
def my_access(request):
    """What the signed-in person can open, and as what - their own grants only."""
    applications = applications_for(request.user).select_related(
        "product__portfolio__organization"
    )
    grants = {
        grant.application_id: grant
        for grant in ApplicationGrant.objects.filter(
            user=request.user, application__in=applications
        )
    }
    rows = [(app, grants[app.pk]) for app in applications.order_by("name") if app.pk in grants]
    return render(
        request,
        "my_access.html",
        {
            "memberships": OrganizationMember.objects.filter(
                user=request.user, organization__active=True
            )
            .select_related("organization")
            .order_by("organization__name"),
            "rows": rows,
            "capabilities": [
                (label, CAPABILITIES[value]) for value, label in ApplicationGrant.Role.choices
            ],
            "approval_capabilities": CAPABILITIES["can_approve"],
        },
    )


@login_required
@require_GET
def platform_console(request):
    """The administration Overview: what needs attention first, then the totals."""
    require_platform_admin(request.user)
    totals = AIUsage.objects.values("currency", "estimated").annotate(
        amount=Sum("amount"), calls=Count("id")
    )
    return render(
        request,
        "platform.html",
        {
            "attention": console.attention(),
            "spend": console.spend_by_organization(),
            "organization_count": Organization.objects.count(),
            "user_count": User.objects.filter(is_active=True).count(),
            "application_count": Application.objects.count(),
            "totals": totals,
        },
    )


@login_required
@require_GET
def platform_organizations(request):
    require_platform_admin(request.user)
    from . import demo_reset

    organizations = list(
        Organization.objects.prefetch_related(
            Prefetch(
                "members",
                queryset=OrganizationMember.objects.filter(is_admin=True)
                .select_related("user")
                .order_by("user__username"),
                to_attr="admin_members",
            )
        ).order_by("name")
    )
    return render(
        request,
        "platform_organizations.html",
        {
            "organizations": organizations,
            "unadministered": {org.pk for org in console.unadministered_organizations()},
            "ownerless": console.ownerless_applications(),
            # Off unless a demonstration instance has asked for it, and already
            # refused by load_config under production.
            "reset_allowed": demo_reset.allowed(),
            "resettable": [
                {"organization": org, "counts": demo_reset.summary(org)}
                for org in organizations
            ]
            if demo_reset.allowed()
            else [],
        },
    )


@login_required
@require_GET
def platform_security(request):
    require_platform_admin(request.user)
    return render(request, "platform_security.html", console.security())


@login_required
@require_GET
def platform_operations(request):
    require_platform_admin(request.user)
    return render(request, "platform_operations.html", console.operations())


@login_required
@require_GET
def platform_settings(request):
    require_platform_admin(request.user)
    return render(request, "platform_settings.html")


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
            administrators = list(form.cleaned_data["administrators"])
            OrganizationMember.objects.bulk_create(
                OrganizationMember(organization=org, user=user, is_admin=True)
                for user in administrators
            )
            audit(
                request.user,
                "organization.created",
                org.pk,
                org,
                {"administrators": [str(user.pk) for user in administrators]},
            )
        messages.success(
            request,
            f"Organization created with {len(administrators)} administrator"
            f"{'' if len(administrators) == 1 else 's'}.",
        )
        return redirect("platform-organizations")
    return render(
        request,
        "organization_admins.html",
        {
            "form": form,
            "title": "New organization",
            "eyebrow": "Platform administration",
            "cancel_url": reverse("platform-organizations"),
        },
    )


@login_required
@require_GET
def organization(request, pk):
    org = organization_for(request.user, pk)
    admin = OrganizationMember.objects.filter(
        organization=org, user=request.user, is_admin=True
    ).exists()
    accessible = list(
        applications_for(request.user).filter(product__portfolio__organization=org)
    )
    # Each card names where it opens: Runbooks for an Operations-only
    # application, as its own menu does. Worked out for the whole list at once.
    labels = knowledge_labels(accessible)
    for app in accessible:
        app.knowledge_label = labels[app.pk]
    return render(
        request,
        "organization.html",
        {
            "organization": org,
            "is_org_admin": admin,
            "portfolios": org.portfolios.prefetch_related("products__applications").order_by(
                "name", "pk"
            ),
            "accessible_applications": accessible,
            # Administering an organization does not grant access to what is in
            # it, so the structure list names applications this person cannot
            # open. Linking those would send them to a 404; the panel marks them
            # instead, which is the honest reading of the same rule.
            "accessible_ids": {app.pk for app in accessible},
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def organization_members(request, pk):
    org = organization_for(request.user, pk, admin=True)
    if request.method == "POST" and request.POST.get("action") in {"approve", "decline"}:
        invitation = get_object_or_404(
            Invitation,
            pk=request.POST.get("invitation"),
            application__product__portfolio__organization=org,
        )
        try:
            if request.POST["action"] == "decline":
                invitations.decline(request.user, invitation.pk)
                messages.success(request, f"Invitation for {invitation.invitee} declined.")
            else:
                secret = invitations.approve(request.user, invitation.pk)
                if secret:
                    request.session[LINK_KEY] = invitation_link(request, invitation, secret)
                else:
                    messages.success(request, f"{invitation.invitee} approved and given access.")
        except ValidationError as error:
            messages.error(request, " ".join(error.messages))
        return redirect("organization-members", pk=pk)
    if request.method == "POST" and request.POST.get("action") == "remove":
        member = get_object_or_404(
            OrganizationMember.objects.select_related("user"),
            organization=org,
            user_id=request.POST.get("user"),
        )
        try:
            grants, tokens = remove_member(request.user, org, member.user)
        except ValidationError as error:
            messages.error(request, " ".join(error.messages))
        else:
            messages.success(
                request,
                f"{member.user.username} removed: {grants} application grant"
                f"{'' if grants == 1 else 's'} and {tokens} API token"
                f"{'' if tokens == 1 else 's'} went with the membership.",
            )
            if member.user == request.user:
                return redirect("dashboard")
        return redirect("organization-members", pk=pk)
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
        {
            "form": form,
            "organization": org,
            "members": org.members.select_related("user"),
            "waiting": invitations.pending_for(request.user, org.pk),
            "link": request.session.pop(LINK_KEY, None),
            "link_hours": invitations.LINK_HOURS,
        },
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
        request,
        "form.html",
        {
            "form": form,
            "title": "New portfolio",
            "eyebrow": org.name,
            "cancel_url": reverse("organization", args=[org.pk]),
        },
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
        request,
        "form.html",
        {
            "form": form,
            "title": "New product",
            "eyebrow": portfolio.name,
            "cancel_url": reverse("organization", args=[org.pk]),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def create_application(request, pk=None, organization_id=None):
    """Create an application, and any portfolio or product it still needs.

    Two entry points, one form. `application-new` carries a product, so both
    levels are already decided. `application-create` carries only the
    organization and lets the form choose or name them - which is what turned
    three pages and three redirects into one submission.
    """
    if pk is not None:
        product = get_object_or_404(Product.objects.select_related("portfolio"), pk=pk)
        org = organization_for(request.user, product.portfolio.organization_id, admin=True)
    else:
        product, org = None, organization_for(request.user, organization_id, admin=True)
    form = ApplicationForm(request.POST or None, organization=org, product=product)
    if request.method == "POST" and form.is_valid():
        values = form.cleaned_data
        with transaction.atomic():
            target = product
            if target is None:
                portfolio = values.get("portfolio") or Portfolio.objects.create(
                    organization=org, name=values["new_portfolio"].strip()
                )
                if not values.get("portfolio"):
                    audit(request.user, "portfolio.created", portfolio.pk, org)
                target = values.get("product")
                if target is None:
                    target = Product.objects.create(
                        portfolio=portfolio, name=values["new_product"].strip()
                    )
                    audit(request.user, "product.created", target.pk, org)
            app = Application.objects.create(
                product=target, name=values["name"], ontology=values.get("ontology") or ""
            )
            ApplicationGrant.objects.create(
                application=app,
                user=values["owner"],
                role="owner",
                # The box is hidden for Operations, so its value is not a choice
                # anyone made there. Kept on for the reason the field states: an
                # owner without it could never grant it if Engineering is
                # switched on later.
                can_approve=values["owner_can_approve"] or values.get("purpose") == "operations",
            )
            if values.get("grant_me_owner") and values["owner"] != request.user:
                # Explicit, never implied. Administering an organization does not
                # grant access to its applications (policy.py), so the creator is
                # otherwise locked out of the application they just made - and
                # cannot reach its Features screen, which is owner-only.
                ApplicationGrant.objects.create(
                    application=app, user=request.user, role="owner", can_approve=False
                )
            # Only unticked features need a row: all_features treats a missing
            # row as enabled, so writing the rest would be noise.
            ApplicationFeature.objects.bulk_create(
                ApplicationFeature(application=app, key=key, enabled=False)
                for key, enabled in form.selected_features().items()
                if not enabled
            )
            audit(
                request.user,
                "application.created",
                app.pk,
                org,
                details={"ontology": app.ontology} if app.ontology else None,
            )
            # Inside the transaction on purpose: an application that exists must
            # have its AI rows. The credential copy is a filesystem write and so
            # cannot roll back, but an unreferenced file under a UUID that is now
            # unused is inert, and never overwrites one mounted deliberately.
            seeded = seed_application_ai(request.user, app)
        note = "Application created with explicit owner access."
        if seeded:
            note += (
                f" It uses the {seeded} credential from setup; "
                "check the model and prices in AI settings."
            )
        messages.success(request, note)
        # Straight to the checklist, because "it exists" is the least useful
        # thing to tell somebody who now has eight things to do. Only when the
        # creator can actually open it: administering an organization does not
        # grant access to its applications, so somebody who granted ownership
        # to a colleague would land on a 404 immediately after a success
        # message. The checklist follows the application's purpose, so it is
        # there whichever part of the product was chosen.
        reachable = (
            feature_enabled("knowledge", app)
            and ApplicationGrant.objects.filter(application=app, user=request.user).exists()
        )
        if reachable:
            return redirect("onboarding", pk=app.pk)
        return redirect("organization", pk=org.pk)
    return render(
        request,
        "application_form.html",
        {
            "form": form,
            "title": "New application",
            "eyebrow": product.name if product else org.name,
            "organization": org,
            "cancel_url": reverse("organization", args=[org.pk]),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def application_access(request, pk):
    app, grant = application_for(request.user, pk, owner=True)
    action = request.POST.get("action") if request.method == "POST" else None
    invite_form = InviteForm(request.POST if action == "invite" else None, auto_id="invite_%s")
    link = None
    if action == "invite" and invite_form.is_valid():
        try:
            invitation = invitations.invite(
                request.user,
                pk,
                invite_form.cleaned_data["invitee"],
                invite_form.cleaned_data["role"],
                invite_form.cleaned_data["can_approve"],
            )
        except ValidationError as error:
            invite_form.add_error(None, error)
        else:
            if invitation is None:
                messages.success(request, "They are already in this organization: access given.")
            elif invitation.status == Invitation.Status.AWAITING:
                messages.success(
                    request, "Invitation sent to the organization administrator for approval."
                )
            elif invitation.status == Invitation.Status.ACCEPTED:
                messages.success(request, "Approved and access given.")
            else:
                return redirect_with_link(request, "application-access", pk, invitation)
            return redirect("application-access", pk=pk)
    elif action == "review":
        grants = access_review.review(request.user, pk)
        messages.success(
            request,
            f"Access confirmed: {grants} grant{'' if grants == 1 else 's'} reviewed. "
            f"Next review due in {access_review.REVIEW_DAYS} days.",
        )
        return redirect("application-access", pk=pk)
    elif action in {"withdraw", "link"}:
        invitation = get_object_or_404(
            Invitation, pk=request.POST.get("invitation"), application=app
        )
        try:
            if action == "withdraw":
                invitations.withdraw(request.user, invitation.pk)
                messages.success(request, f"Invitation for {invitation.invitee} withdrawn.")
            else:
                link = invitation_link(request, invitation, invitations.issue_link(
                    request.user, invitation.pk
                ))
        except ValidationError as error:
            messages.error(request, " ".join(error.messages))
        if link is None:
            return redirect("application-access", pk=pk)
    form = GrantForm(request.POST if action in {"save", "revoke"} else None, application=app)
    if action in {"save", "revoke"} and form.is_valid():
        target = form.cleaned_data["user"]
        can_approve = form.cleaned_data["can_approve"]
        if not grant.can_approve:
            # Their checkbox is disabled, so it posts nothing: keep what the
            # person already holds rather than read the silence as "no".
            existing = ApplicationGrant.objects.filter(application=app, user=target).first()
            can_approve = bool(existing and existing.can_approve)
        try:
            change_grant(
                request.user,
                pk,
                target,
                form.cleaned_data["role"],
                can_approve,
                revoke=action == "revoke",
                expires_at=form.expires_at(),
            )
        except ValidationError as error:
            form.add_error(None, error)
        else:
            messages.success(request, f"Access for {target.username} updated.")
            return redirect("application-access", pk=pk)
    elif action not in {None, "invite", "save", "revoke"}:
        return HttpResponse("Choose a valid action.", status=400)
    return render(
        request,
        "access.html",
        {
            "application": app,
            "form": form,
            "grants": app.grants.select_related("user").order_by("user__username"),
            "grant": grant,
            "roles": ApplicationGrant.Role.choices,
            "invite_form": invite_form,
            "invitations": app.invitations.filter(status__in=invitations.PENDING),
            "link": link or request.session.pop(LINK_KEY, None),
            "link_hours": invitations.LINK_HOURS,
            "review": access_review.review_state(app),
            "capabilities": [
                (label, CAPABILITIES[value]) for value, label in ApplicationGrant.Role.choices
            ],
            "approval_capabilities": CAPABILITIES["can_approve"],
            "earliest_expiry": (timezone.now() + timedelta(days=1)).date(),
        },
    )


#: Where a just-issued invitation link waits for the page the redirect lands on.
#: The session is the server's, and the value is removed the first time it is
#: read, so the link is shown once - never in a URL, never stored in the database.
LINK_KEY = "invitation_link"


def invitation_link(request, invitation, secret):
    return {
        "invitee": invitation.invitee,
        "url": request.build_absolute_uri(
            reverse("invitation-accept", args=[invitation.pk, secret])
        ),
    }


def redirect_with_link(request, name, pk, invitation):
    secret = invitations.issue_link(request.user, invitation.pk)
    request.session[LINK_KEY] = invitation_link(request, invitation, secret)
    return redirect(name, pk=pk)


@require_http_methods(["GET", "POST"])
def accept_invitation(request, pk, secret):
    """Make the account an approved invitation asked for. No sign-in needed."""
    invitation = invitations.opened(pk, secret)
    form = AcceptInvitationForm(request.POST or None)
    if invitation is not None and request.method == "POST" and form.is_valid():
        try:
            user = invitations.accept(pk, secret, form)
        except ValidationError as error:
            form.add_error(None, error)
        else:
            messages.success(request, f"Account {user.username} made. Sign in with it now.")
            return redirect("login")
    return render(
        request,
        "invitation_accept.html",
        {"invitation": invitation, "form": form},
    )


#: The audit log's sortable columns: newest first unless a heading says otherwise.
AUDIT_COLUMNS = {
    "time": sorting.Column("Time", "desc", field="created_at"),
    "actor": sorting.Column("Actor", field="actor__username"),
    "organization": sorting.Column("Organization", field="organization__name"),
    "action": sorting.Column("Action", field="action"),
    "resource": sorting.Column("Resource", field="resource_id"),
}
#: AI usage receipts: time, who answered, for what, and what it cost.
USAGE_COLUMNS = {
    "time": sorting.Column("Time (UTC)", "desc", field="created_at"),
    "model": sorting.Column("Provider / model", field="model"),
    "task": sorting.Column("Task", field="purpose"),
    "input": sorting.Column("Input", "desc", field="input_tokens"),
    "output": sorting.Column("Output", "desc", field="output_tokens"),
    "cost": sorting.Column("Cost", "desc", field="amount"),
}


def sorted_page(request, queryset, columns, default, per_page):
    """One page of a queryset in the order its headings ask for, and what draws them."""
    sort, direction = sorting.choose(request, columns, default)
    queryset = sorting.order_queryset(queryset, columns[sort], direction)
    return {
        "page": Paginator(queryset, per_page).get_page(request.GET.get("page")),
        "columns": sorting.headers(request, columns, sort, direction),
        "extra_query": urlencode(sorting.carried(sort, direction, columns, default)),
    }


@login_required
@require_GET
def audit_log(request):
    org_ids = OrganizationMember.objects.filter(
        user=request.user, is_admin=True, organization__active=True
    ).values("organization_id")
    scope = Q(organization_id__in=org_ids)
    if request.user.is_platform_admin:
        # Every control-plane event, in every organization, and no application
        # event stream - unless they also administer that organization, which
        # being a platform administrator neither grants nor takes away.
        scope |= control_plane()
    events = AuditEvent.objects.filter(scope).select_related("actor", "organization")
    return render(
        request,
        "audit.html",
        sorted_page(request, events, AUDIT_COLUMNS, "time", 30),
    )


@login_required
@require_GET
def usage(request, pk):
    app, grant = application_for(request.user, pk)
    if not feature_enabled("usage_reports", app):
        # Same reasoning as chat_settings above: a disabled feature is 403.
        raise PermissionDenied("This feature is disabled.")
    from .budget import status as budget_status

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
            "budget": budget_status(app.pk),
            **sorted_page(request, records, USAGE_COLUMNS, "time", 30),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def onboarding_connectors(request, pk):
    """Onboarding's one question: which systems this application imports from.

    Owner-only, like the Features screen these are switches on. Every kind gets
    an explicit row, so "answered, nothing ticked" differs from "not asked yet",
    where every kind is still allowed. Ticked first: every kind, so saving the
    question as offered switches nothing off.
    """
    from .connector_kinds import KINDS
    from .readiness import chosen_connectors
    from .services import connector_feature

    app, _ = application_for(request.user, pk, owner=True)
    if request.method == "POST":
        wanted = {kind: f"connector_{kind}" in request.POST for kind in KINDS}
        organization = app.product.portfolio.organization
        with transaction.atomic():
            for kind, enabled in wanted.items():
                key = connector_feature(kind)
                row, created = ApplicationFeature.objects.get_or_create(
                    application=app, key=key, defaults={"enabled": enabled}
                )
                if created:
                    if enabled:
                        # Written so the question reads as answered, but it
                        # matches what a missing row meant: nothing changed.
                        continue
                elif row.enabled == enabled:
                    continue
                else:
                    row.enabled = enabled
                    row.save(update_fields=["enabled"])
                audit(
                    request.user,
                    "feature.enabled" if enabled else "feature.disabled",
                    key,
                    organization,
                )
        names = [KINDS[kind].label for kind, on in wanted.items() if on]
        messages.success(
            request,
            f"Connectors chosen: {', '.join(names)}." if names else "No connectors chosen.",
        )
        return redirect("onboarding", pk=app.pk)
    chosen = chosen_connectors(app)
    if chosen is None:
        # Every kind, as a missing row already means: a question nobody has
        # answered must not switch a connector off by being saved as offered.
        chosen = set(KINDS)
    return render(
        request,
        "onboarding_connectors.html",
        {
            "application": app,
            "kinds": [(kind, spec, kind in chosen) for kind, spec in KINDS.items()],
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
        organization = app.product.portfolio.organization if app else None

        def apply(key, enabled):
            """Write one switch and audit it only when it actually changed."""
            if app:
                row, created = ApplicationFeature.objects.get_or_create(
                    application=app, key=key, defaults={"enabled": enabled}
                )
            else:
                row, created = FeatureSwitch.objects.get_or_create(
                    key=key, defaults={"enabled": enabled}
                )
            if not created:
                if row.enabled == enabled:
                    return False
                row.enabled = enabled
                row.save(update_fields=["enabled"])
            elif enabled:
                # A new row that matches the default changes nothing worth recording.
                return False
            audit(
                request.user,
                "feature.enabled" if enabled else "feature.disabled",
                key,
                organization,
            )
            return True

        # The page submits every checkbox at once, so one decision reaches the
        # server as one request instead of a full page reload per feature.
        if request.POST.get("features_declared"):
            switchable = {key for key, (_, available) in FEATURES.items() if available}
            wanted = {key: f"feature_{key}" in request.POST for key in switchable}
            with transaction.atomic():
                changed = sum(apply(key, enabled) for key, enabled in wanted.items())
            messages.success(
                request,
                "Feature settings saved." if changed else "No feature settings changed.",
            )
            return redirect("application-features", pk=pk) if app else redirect("features")

        # The original single-toggle form, kept working: it is a real POST target
        # that any bookmarked page or script may still use.
        key = request.POST.get("feature")
        state = request.POST.get("state")
        if key not in FEATURES or not FEATURES[key][1] or state not in {"on", "off"}:
            return HttpResponse("Invalid or unavailable feature.", status=400)
        with transaction.atomic():
            apply(key, state == "on")
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
                "area": area_of(key),
            }
        )
    # Grouped by what each feature is part of, in the order a reader decides:
    # what the application is for, then where it imports from, then the rest.
    groups = [
        (AREA_LABELS[area], AREA_ICONS[area], [row for row in rows if row["area"] == area])
        for area in ("engineering", "operations", "connectors", "shared")
    ]
    return render(
        request,
        "features.html",
        {
            "features": rows,
            "groups": [group for group in groups if group[2]],
            "application": app,
            "purposes": [AREA_LABELS[purpose] for purpose in purposes(app)] if app else [],
        },
    )


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
            # The origin callers will use, so the page can show - and copy - a
            # complete URL rather than a path the reader has to assemble.
            "base": f"{request.scheme}://{request.get_host()}",
            # Shown either way: the address is worth knowing before it is
            # switched on, and hiding it would make the page look like the
            # endpoint does not exist.
            "chat_api_enabled": feature_enabled("chat_api", app),
        },
    )
