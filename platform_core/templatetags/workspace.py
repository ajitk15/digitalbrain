from django import template
from django.urls import reverse

from platform_core.models import ApplicationGrant
from platform_core.policy import applications_for, organizations_for
from platform_core.services import feature_enabled

register = template.Library()


@register.inclusion_tag("application_nav.html", takes_context=True)
def application_nav(context):
    return {"application": context.get("application")}


@register.inclusion_tag("application_menu.html", takes_context=True)
def application_menu(context):
    app = context["application"]
    request = context["request"]
    grant = ApplicationGrant.objects.filter(application=app, user=request.user).first()
    current = request.resolver_match.url_name
    definitions = [
        ("Documents", "application", None, {"application", "documents", "document-detail"}),
        ("Knowledge", "graph", "knowledge", {"knowledge", "knowledge-detail", "graph"}),
        ("Chat", "chat", "chat", {"chat"}),
        ("Code Factory", "plans", "code_factory", {"plans", "plan-detail"}),
        ("AI costs", "usage", "usage_reports", {"usage"}),
    ]
    if grant and grant.role == "owner":
        definitions += [
            ("AI settings", "ai-settings", None, {"ai-settings"}),
            ("Connectors", "connectors", "connectors", {"connectors"}),
            ("People & access", "application-access", None, {"application-access"}),
            ("Settings", "application-features", None, {"application-features"}),
        ]
    items = [
        {"label": label, "url": reverse(route, args=[app.pk]), "current": current in routes}
        for label, route, feature, routes in definitions
        if feature is None or feature_enabled(feature, app)
    ]
    return {"application": app, "items": items}


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
