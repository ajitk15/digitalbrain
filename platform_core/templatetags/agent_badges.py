"""`{% agent_badge application "code-review" %}`: where an agent runs, said beside the
control that starts it. A link to that agent's page, opened as a popup
(`data-modal`), saying whether it is on here. See `platform_core/agent_catalog.py`."""

from django import template
from django.template.loader import render_to_string

from ..agent_catalog import get, status

register = template.Library()


@register.simple_tag
def agent_badge(application, key):
    agent = get(key)
    if agent is None:
        raise template.TemplateSyntaxError(f"No agent named {key!r}.")
    state, label, detail = status(agent, application)
    return render_to_string(
        "_agent_badge.html",
        {"application": application, "agent": agent, "state": state, "label": label,
         "detail": detail},
    )
