"""Where agents are, on the screens they serve. See `platform_core/agent_catalog.py`.

`{% agent_badge application "code-review" %}` sits beside the control that starts an
agent and says whether its next run will be one. `{% agent_tag application
"code-review" calls %}` sits on what an agent made - an answer, a draft, a finding,
a Code Factory step, a triage run - when it made `calls` tool calls to make it, and
renders nothing otherwise: a single call to the model is not an agent at work.
Both open that agent's page as a popup (`data-modal`). `agent_badge_for` (AI
settings, by task) and `agent_badges_for_feature` (Features, by switch) find the
agent for a row that names it another way."""

from django import template
from django.template.loader import render_to_string

from ..agent_catalog import by_purpose, for_feature, get, status

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
def agent_tag(application, key, calls, link=True):
    """`calls` is a count, or a row carrying `agent_calls` (set by its list view
    in one query). `link=False` inside a row that is itself a link."""
    agent = get(key)
    if agent is None:
        raise template.TemplateSyntaxError(f"No agent named {key!r}.")
    if not isinstance(calls, int):
        calls = getattr(calls, "agent_calls", 0)
    if not isinstance(calls, int) or isinstance(calls, bool) or calls <= 0:
        return ""
    return render_to_string(
        "_agent_tag.html",
        {"application": application, "agent": agent, "calls": calls, "link": link},
    )


@register.simple_tag
def agent_badge_for(application, purpose):
    """The badge of the agent that runs under an AI settings task, or nothing."""
    agent = by_purpose(purpose)
    return agent_badge(application, agent.key) if agent else ""


@register.simple_tag
def agent_badges_for_feature(application, feature):
    """The badge of every agent a feature runs - on Features, beside its switch."""
    from django.utils.html import format_html_join

    return format_html_join(
        "", "{}", ((agent_badge(application, agent.key),) for agent in for_feature(feature))
    )
