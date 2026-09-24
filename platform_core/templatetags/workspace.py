from django import template
from django.db.models import Exists, OuterRef, Q
from django.urls import reverse

from platform_core.models import Application, ApplicationGrant, OrganizationMember, Portfolio
from platform_core.policy import applications_for, organizations_for
from platform_core.services import feature_enabled

register = template.Library()


@register.inclusion_tag("application_nav.html", takes_context=True)
def application_nav(context):
    """The breadcrumbs, and the setup strip while an application is unfinished.

    The strip rather than a forced redirect: making every screen return here
    would mean seven views honouring a `next` parameter, which is both invasive
    and the usual way an open redirect gets in. This keeps the checklist one
    click away from wherever the work actually happens, and lets somebody look
    around without being dragged back.

    It stops at `analysis_ready` rather than at complete, so an application
    nobody intends to deliver from is not nagged forever.
    """
    from platform_core.readiness import record_completion, setup

    app = context.get("application")
    request = context.get("request")
    user = getattr(request, "user", None)
    state = None
    # `setup_completed_at` is the cheap half of this check and comes first: an
    # application that finished onboarding costs this tag nothing at all, which
    # is why the milestone is recorded rather than recomputed. Answering "is it
    # unfinished?" takes seven queries, and a page render has a budget that
    # test_feature_cache pins.
    if (
        app is not None
        and app.setup_completed_at is None
        and user is not None
        and getattr(user, "is_authenticated", False)
        # Only for somebody who could act on it: a grant, and the feature that
        # the checklist itself lives behind.
        and feature_enabled("code_factory", app)
        and ApplicationGrant.objects.filter(application=app, user=user).exists()
    ):
        found = setup(app)
        if found.analysis_ready:
            record_completion(app, found)
        else:
            state = found
    return {"application": app, "setup": state}


def settings_sections(app, grant):
    """The settings screens this user may reach, in display order.

    Single source of truth for both the Settings sub-navigation and the Settings
    tab's own visibility. Each screen keeps its own URL and its own authorization
    check; this only decides what to link to. Note that AI costs is gated on a
    feature switch rather than on ownership, so a contributor can reach Settings
    while an owner-only section list stays empty for them.
    """
    sections = []
    if grant and grant.role == "owner":
        sections.append(("AI settings", "ai-settings", {"ai-settings"}, "sliders"))
    if feature_enabled("usage_reports", app):
        sections.append(("AI costs", "usage", {"usage"}, "cost"))
    if grant and grant.role == "owner":
        if feature_enabled("connectors", app):
            sections.append(
                (
                    "Connectors",
                    "connectors",
                    {"connectors", "connector-new", "connector-edit"},
                    "plug",
                )
            )
        sections.append(("Credentials", "credentials", {"credentials"}, "lock"))
        sections.append(("People & access", "application-access", {"application-access"}, "people"))
        sections.append(("Features", "application-features", {"application-features"}, "toggle"))
        if feature_enabled("chat", app):
            sections.append(("Chat history", "chat-settings", {"chat-settings"}, "history"))
    if grant:
        # Any member with application access may hold a token; it can never do
        # more than they can.
        sections.append(("API access", "api-tokens", {"api-tokens"}, "key"))
    return sections


@register.inclusion_tag("application_menu.html", takes_context=True)
def application_menu(context):
    app = context["application"]
    request = context["request"]
    grant = ApplicationGrant.objects.filter(application=app, user=request.user).first()
    current = request.resolver_match.url_name
    definitions = [
        (
            "Knowledge",
            "graph",
            "knowledge",
            "graph",
            {
                "knowledge",
                "knowledge-detail",
                "graph",
                "application",
                "documents",
                "document-detail",
                "document-delete",
            },
        ),
        ("Code Graph", "code-graph", "code_graph", "graph", {"code-graph", "code-file"}),
        # Knowledge, Code Graph, Code Factory: what an application knows, what
        # its code is, and the pipeline that reads both. Chat sits after them
        # because it asks questions of that knowledge rather than building it,
        # and putting it between the two graphs and the thing that consumes
        # them broke the order somebody sets an application up in.
        (
            "Code Factory",
            "plans",
            "code_factory",
            "code",
            {"plans", "plan-detail", "runs", "run-detail", "onboarding"},
        ),
        ("Chat", "chat", "chat", "chat", {"chat"}),
    ]
    items = [
        {
            "label": label,
            "icon": name,
            "url": reverse(route, args=[app.pk]),
            "current": current in routes,
        }
        for label, route, feature, name, routes in definitions
        if feature is None or feature_enabled(feature, app)
    ]
    sections = settings_sections(app, grant)
    if sections:
        # The label is always "Settings", never the first section's name: a
        # contributor with usage reports on would otherwise render "AI costs" in
        # the top menu, which is exactly what the feature-gating test forbids.
        routes = {route for _, _, section_routes, _ in sections for route in section_routes}
        items.append(
            {
                "label": "Settings",
                "icon": "settings",
                "url": reverse(sections[0][1], args=[app.pk]),
                "current": current in routes,
            }
        )
    return {"application": app, "items": items}


@register.inclusion_tag("settings_nav.html", takes_context=True)
def settings_nav(context):
    """Sub-navigation across the settings screens, which keep separate URLs."""
    app = context["application"]
    request = context["request"]
    grant = ApplicationGrant.objects.filter(application=app, user=request.user).first()
    current = request.resolver_match.url_name
    return {
        "items": [
            {
                "label": label,
                "icon": name,
                "url": reverse(route, args=[app.pk]),
                "current": current in routes,
            }
            for label, route, routes, name in settings_sections(app, grant)
        ]
    }


@register.inclusion_tag("organization_tree.html", takes_context=True)
def organization_tree(context):
    request = context["request"]
    selected_app = context.get("application")
    selected_org = context.get("organization")
    org_id = selected_app.organization_id if selected_app else getattr(selected_org, "pk", None)
    # Only organizations this person administers get a "+": showing it anywhere
    # else would open a popup onto a 404, since creating requires org admin.
    # Each "+" creates the next level down - portfolio under an organization,
    # product under a portfolio, application under a product - so the ids of the
    # intermediate levels have to travel with the tree, not just their names.
    administered = set(
        OrganizationMember.objects.filter(user=request.user, is_admin=True).values_list(
            "organization_id", flat=True
        )
    )
    tree = {
        org.pk: {
            "id": org.pk,
            "name": org.name,
            "portfolios": {},
            "current": org.pk == org_id,
            "selected": org.pk == org_id and not selected_app,
            "can_create": org.pk in administered,
        }
        for org in organizations_for(request.user).order_by("name", "id")
    }
    # Scaffolding first, and only where this person administers the organization.
    # The tree used to be assembled purely from applications, which meant a
    # portfolio holding nothing yet could not appear at all: an admin created one
    # and the sidebar did not change, so the create looked as though it had
    # failed. Empty levels are exactly the ones whose "+" is needed next.
    #
    # Portfolios, products and application names are the organization's own
    # shape, and an admin already sees all of them in the organization page's
    # Structure panel. An application this admin holds no grant for is listed
    # but not linked, because the link would land on a 404. Naming it is what
    # the Structure panel already does; opening it still takes a grant, and
    # `accessible` is decided by `applications_for` alone.
    for portfolio in (
        Portfolio.objects.filter(organization_id__in=administered & set(tree))
        .prefetch_related("products")
        .order_by("name", "id")
    ):
        branch = tree[portfolio.organization_id]["portfolios"].setdefault(
            portfolio.pk,
            {"id": portfolio.pk, "name": portfolio.name, "products": {}, "current": False},
        )
        for product in sorted(portfolio.products.all(), key=lambda item: (item.name, item.pk)):
            branch["products"].setdefault(
                product.pk,
                {
                    "id": product.pk,
                    "name": product.name,
                    "applications": [],
                    "current": False,
                },
            )

    # One query for both: what this person was granted, and - only in
    # organizations they administer - everything else, marked as not openable.
    granted = applications_for(request.user)
    listed = (
        Application.objects.filter(
            Q(pk__in=granted.values("pk"))
            | Q(product__portfolio__organization_id__in=administered & set(tree))
        )
        .annotate(accessible=Exists(granted.filter(pk=OuterRef("pk"))))
        .select_related("product__portfolio")
        .order_by("product__portfolio__name", "product__name", "name", "id")
    )
    for app in listed:
        org = tree.get(app.organization_id)
        if org is None:
            continue
        portfolio = app.product.portfolio
        branch = org["portfolios"].setdefault(
            portfolio.pk,
            {"id": portfolio.pk, "name": portfolio.name, "products": {}, "current": False},
        )
        product = branch["products"].setdefault(
            app.product_id,
            {
                "id": app.product_id,
                "name": app.product.name,
                "applications": [],
                "current": False,
            },
        )
        current = bool(app.accessible and selected_app and app.pk == selected_app.pk)
        branch["current"] |= current
        product["current"] |= current
        product["applications"].append(
            {
                "id": app.pk,
                "name": app.name,
                "current": current,
                "accessible": app.accessible,
                "active": app.active,
            }
        )
    for org in tree.values():
        org["portfolios"] = list(org["portfolios"].values())
        for portfolio in org["portfolios"]:
            portfolio["products"] = list(portfolio["products"].values())
    return {"tree": list(tree.values())}


#: Per-stage wording: (waiting, active, done, failed). A download stage exists
#: only for documents that arrive as a link; an upload already has its bytes.
_STEP_LABELS = {
    "download": ("Waiting to download", "Downloading", "Downloaded", "Download failed"),
    "upload": ("Uploaded", "Uploaded", "Uploaded", "Upload failed"),
    "convert": (
        "Queued for conversion",
        "Converting to Markdown",
        "Converted",
        "Conversion failed",
    ),
    "ready": ("Ready to search", "Ready to search", "Ready to search", "Not searchable"),
}
_STATE_INDEX = {"waiting": 0, "active": 1, "done": 2, "failed": 3, "todo": 0}


@register.simple_tag
def document_steps(doc):
    """Where a document is on its way in, one entry per stage.

    Read from `Document.status` alone, so it can never claim more than the row
    says. A failure is placed on the download stage when nothing was ever
    downloaded (`sha256` is written with the bytes) and on conversion otherwise,
    which is the same test `documents.retry` uses to decide where to send it back.
    """
    first = "upload" if doc.origin == "upload" else "download"
    status = doc.status
    downloaded = first == "upload" or bool(doc.sha256)
    if status == "pending":
        states = ["waiting", "todo", "todo"]
    elif status == "fetching":
        states = ["active", "todo", "todo"]
    elif status in {"quarantined", "queued"}:
        states = ["done", "waiting", "todo"]
    elif status == "converting":
        states = ["done", "active", "todo"]
    elif status == "ready":
        states = ["done", "done", "done"]
    elif status in {"failed", "rejected"}:
        states = ["done", "failed", "todo"] if downloaded else ["failed", "todo", "todo"]
    else:
        return []
    steps = []
    for key, state in zip((first, "convert", "ready"), states, strict=True):
        label = _STEP_LABELS[key][_STATE_INDEX[state]]
        if key == "convert" and status == "rejected":
            label = "Rejected by scanner"
        steps.append({"key": key, "state": state, "label": label})
    return steps


@register.filter
def source_label(url):
    """A readable origin for a link-sourced document.

    The stored URL is the download address, which for GitHub is a long
    raw.githubusercontent path. What a reader wants is where it came from, so
    GitHub collapses to owner/repo and everything else to host plus a short path.
    """
    from urllib.parse import urlparse

    if not url:
        return ""
    parsed = urlparse(url)
    host = (parsed.hostname or "").removeprefix("www.")
    parts = [p for p in (parsed.path or "").split("/") if p]
    if host in {"raw.githubusercontent.com", "github.com"} and len(parts) >= 2:
        tail = parts[-1] if len(parts) > 2 else ""
        return f"{parts[0]}/{parts[1]}" + (f" · {tail}" if tail else "")
    if not parts:
        return host
    return f"{host}/{parts[-1]}" if len(parts[-1]) <= 40 else host
