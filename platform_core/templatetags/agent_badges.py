"""Where agents are, on the screens they serve. See `platform_core/agent_catalog.py`.

`{% agent_badge application "code-review" %}` sits beside the control that starts an
agent and says whether its next run will be one. `{% agent_tag application
"code-review" calls %}` sits on what an agent made - an answer, a draft, a finding,
a Code Factory step, a triage run - when it made `calls` tool calls to make it, and
renders nothing otherwise: a single call to the model is not an agent at work.
Both open that agent's page as a popup (`data-modal`)."""

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


@register.simple_tag
def agent_tag(application, key, calls):
    agent = get(key)
    if agent is None:
        raise template.TemplateSyntaxError(f"No agent named {key!r}.")
    if not isinstance(calls, int) or isinstance(calls, bool) or calls <= 0:
        return ""
    return render_to_string(
        "_agent_tag.html", {"application": application, "agent": agent, "calls": calls}
    )
