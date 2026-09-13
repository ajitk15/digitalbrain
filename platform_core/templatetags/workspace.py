from django import template
from django.urls import reverse

from platform_core.models import ApplicationGrant
from platform_core.policy import applications_for, organizations_for
from platform_core.services import feature_enabled

register = template.Library()


@register.inclusion_tag("application_nav.html", takes_context=True)
def application_nav(context):
    return {"application": context.get("application")}


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
        sections.append(("AI settings", "ai-settings", {"ai-settings"}))
    if feature_enabled("usage_reports", app):
        sections.append(("AI costs", "usage", {"usage"}))
    if grant and grant.role == "owner":
        if feature_enabled("connectors", app):
            sections.append(("Connectors", "connectors", {"connectors"}))
        sections.append(("People & access", "application-access", {"application-access"}))
        sections.append(("Features", "application-features", {"application-features"}))
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
        ("Chat", "chat", "chat", {"chat"}),
        ("Code Factory", "plans", "code_factory", {"plans", "plan-detail"}),
    ]
    items = [
        {
            "label": label,
            "icon": label,
            "url": reverse(route, args=[app.pk]),
            "current": current in routes,
        }
        for label, route, feature, routes in definitions
        if feature is None or feature_enabled(feature, app)
    ]
    sections = settings_sections(app, grant)
    if sections:
        # The label is always "Settings", never the first section's name: a
        # contributor with usage reports on would otherwise render "AI costs" in
        # the top menu, which is exactly what the feature-gating test forbids.
        routes = {route for _, _, section_routes in sections for route in section_routes}
        items.append(
            {
                "label": "Settings",
                "icon": "Settings",
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
                "url": reverse(route, args=[app.pk]),
                "current": current in routes,
            }
            for label, route, routes in settings_sections(app, grant)
        ]
    }


@register.inclusion_tag("organization_tree.html", takes_context=True)
def organization_tree(context):
    request = context["request"]
    selected_app = context.get("application")
    selected_org = context.get("organization")
    org_id = selected_app.organization_id if selected_app else getattr(selected_org, "pk", None)
    tree = {
        org.pk: {
            "id": org.pk,
            "name": org.name,
            "portfolios": {},
            "current": org.pk == org_id,
            "selected": org.pk == org_id and not selected_app,
        }
        for org in organizations_for(request.user).order_by("name", "id")
    }
    for app in applications_for(request.user).order_by(
        "product__portfolio__name", "product__name", "name", "id"
    ):
        org = tree.get(app.organization_id)
        if org is None:
            continue
        portfolio = app.product.portfolio
        branch = org["portfolios"].setdefault(
            portfolio.pk, {"name": portfolio.name, "products": {}, "current": False}
        )
        product = branch["products"].setdefault(
            app.product_id, {"name": app.product.name, "applications": [], "current": False}
        )
        current = bool(selected_app and app.pk == selected_app.pk)
        branch["current"] |= current
        product["current"] |= current
        product["applications"].append({"id": app.pk, "name": app.name, "current": current})
    for org in tree.values():
        org["portfolios"] = list(org["portfolios"].values())
        for portfolio in org["portfolios"]:
            portfolio["products"] = list(portfolio["products"].values())
    return {"tree": list(tree.values())}
