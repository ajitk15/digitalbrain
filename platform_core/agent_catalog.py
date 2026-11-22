"""The agents this platform runs, said where they run (the agent badge and its popup).

A closed registry like `services.FEATURES`: each `Agent` names the gateway purpose
its runs spend under, the feature its screen needs, the setting that gives it
tools (none for an agent that always has them), where it runs, and its limits.
What it may read is never written here - it is `registry.for_scope` for its
purpose, the list the harness itself hands the model - so the popup cannot
describe a tool the agent does not have, or miss one it does.

Two readers: `{% agent_badge %}` (`templatetags/agents.py`), drawn beside every
control that starts an agent, and the pages `agents` and `agent`, which the badge
opens as a popup (`data-modal`) and which render and work on their own. Nothing
here starts an agent, changes a setting or shows what an agent read: activity is
`AgentStep` counts by tool and outcome, the shape the trajectory already keeps.
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta
from types import SimpleNamespace

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_GET

#: How far back a popup counts an agent's steps.
ACTIVITY_DAYS = 30

#: Every agent keeps these, whatever it is for (`agent_runtime/harness.py`).
SHARED_SAFEGUARDS = (
    "Reads only: every tool it is given is read-only, and it never writes, approves, "
    "publishes or deletes anything.",
    "Bound to this application and the person who started it before it begins; it can "
    "narrow what it reads, never widen it. Another application's record reads as "
    "not found.",
    "Access is checked again on every tool call, and once more after the answer.",
    "Every turn is paid for up front within the monthly budget, and gets its own receipt.",
    "Each step is recorded - which tool, the shape of its arguments, what came of it - "
    "never the text it asked for or read.",
)


@dataclass(frozen=True)
class Agent:
    key: str
    name: str
    #: The gateway purpose its runs spend under, and the AI settings task that
    #: names its model.
    purpose: str
    #: The application feature its screen needs.
    feature: str
    #: The setting that gives it tools; empty for an agent that always has them.
    switch: str
    #: Where in the application it is started, and by what.
    trigger: str
    #: Where it runs on the server.
    where: str
    does: str
    turns: int
    tool_calls: int
    #: Which `AgentStep` link records its runs.
    steps_field: str
    safeguards: tuple = ()
    measured: str = ""
    kept_back: bool = False
    #: When its tools are off, what it does instead.
    one_call: str = ""
    extra: dict = field(default_factory=dict)


def _limits():
    from . import code_factory_agents, code_review, learning_draft, serviceops_triage
    from .agent_runtime.harness import TURNS
    from .agent_runtime.tools import MAX_TOOL_CALLS

    return {
        "chat": (TURNS, MAX_TOOL_CALLS),
        "knowledge_upkeep": (learning_draft.TOOL_TURNS, learning_draft.TOOL_CALLS),
        "code_review": (code_review.TOOL_TURNS, code_review.TOOL_CALLS),
        "plan_drafting": (code_factory_agents.TOOL_TURNS, code_factory_agents.TOOL_CALLS),
        "serviceops_triage": (serviceops_triage.TOOL_TURNS, serviceops_triage.TOOL_CALLS),
    }


def _agents():
    limits = _limits()
    return (
        Agent(
            key="chat",
            name="Chat agent",
            purpose="chat",
            feature="chat",
            switch="",
            trigger="Each question asked in Chat.",
            where="On the web server, on a thread of its own for each answer, streaming "
            "its words to the page as they come.",
            does="Answers a question from the application's own sources. It starts from "
            "what search finds for the question, then decides whether to search again "
            "with other words or read a source further before it answers.",
            turns=limits["chat"][0],
            tool_calls=limits["chat"][1],
            steps_field="message",
            kept_back=True,
            safeguards=(
                "An answer cites only sources that are still active, unchanged and hold "
                "the quoted passage; anything else is dropped before it is shown.",
                "Restricted sources are never read, and a source system's own permissions "
                "decide what may be cited for you.",
                "Out of turns, a turn kept back answers from what it has read, rather "
                "than leaving you with an error.",
            ),
        ),
        Agent(
            key="draft-a-fix",
            name="Knowledge upkeep agent",
            purpose="knowledge_upkeep",
            feature="knowledge",
            switch="",
            trigger="“Draft a fix” on a waiting question in Knowledge → Learned, pressed "
            "by an owner or contributor.",
            where="On the web server, inside the request the button sends.",
            does="Finds the source a question nobody could answer should cite. It "
            "searches with the question's words and others, reads what it finds, and "
            "proposes one source with a sentence quoted from it - or says what the "
            "library is missing.",
            turns=limits["knowledge_upkeep"][0],
            tool_calls=limits["knowledge_upkeep"][1],
            steps_field="candidate",
            kept_back=True,
            safeguards=(
                "A proposal counts only for a source it was shown, which you can see, "
                "and whose text holds the quote word for word; naming a source it never "
                "read is dropped as a guess.",
                "It decides nothing: it fills in “Should cite”, and a question joins the "
                "benchmark only when a person adds it.",
                "Checked again every time the page is drawn.",
            ),
            measured="manage.py learning_upkeep counts how often a drafted question is "
            "accepted, and with the drafted source, against questions decided without one.",
        ),
        Agent(
            key="code-review",
            name="Code review agent",
            purpose="code_review",
            feature="code_review",
            switch="CODE_REVIEW_AGENT_TOOLS",
            trigger="“Ask AI to review” on a code review.",
            where="On the code-graph worker lane - the sandbox container in a split "
            "deployment - as the person who pressed it.",
            does="Reviews one change. It reads each changed file's whole diff, asks which "
            "files depend on it, opens them, and reports problems - a caller the change "
            "breaks, a requirement it no longer meets - quoting the code that shows each.",
            turns=limits["code_review"][0],
            tool_calls=limits["code_review"][1],
            steps_field="review",
            safeguards=(
                "Reads only this review's change and the code snapshot it is based on.",
                "Every finding must quote what it read exactly; a finding with a quote "
                "that is not there is dropped, and each quote is checked again whenever "
                "the review is shown.",
                "Nothing is posted to GitHub.",
            ),
            measured="manage.py review_pack (paid) runs labelled CarePath changes with "
            "tools and without, before it is switched on in production.",
            one_call="Asks once over the diff and excerpts chosen for it beforehand.",
        ),
        Agent(
            key="code-factory",
            name="Code Factory agent",
            purpose="plan_drafting",
            feature="code_factory",
            switch="CODE_FACTORY_AGENT_TOOLS",
            trigger="The implementation, test and review steps of a Code Factory run "
            "pinned to a code snapshot.",
            where="On the factory worker lane - the sandbox container in a split "
            "deployment.",
            does="Writes the change a run's approved plan asks for. Before writing, it "
            "looks things up in the run's code: lists and reads files, finds symbols, "
            "and checks what depends on a file and which tests cover it.",
            turns=limits["plan_drafting"][0],
            tool_calls=limits["plan_drafting"][1],
            steps_field="run",
            safeguards=(
                "Reads only the run's own code snapshot, by looking files up among its "
                "indexed rows: no path it writes reaches the disk.",
                "What a run may change is the same with or without tools: the same "
                "files, collected the same way, reviewed by a person before delivery.",
            ),
            measured="manage.py factory_pack (paid) runs labelled tickets with tools "
            "and without, before it is switched on in production.",
            one_call="Asks once over files chosen beforehand.",
        ),
        Agent(
            key="triage",
            name="ServiceOps triage agent",
            purpose="serviceops_triage",
            feature="service_ops",
            switch="SERVICEOPS_TRIAGE_AGENT_TOOLS",
            trigger="“Triage this incident” on an incident.",
            where="On the serviceops worker lane, as the person who asked.",
            does="Suggests what caused an incident. It starts from the incident's "
            "evidence pack and may look at a similar incident's neighbourhood, the "
            "published runbooks and other sources before proposing up to three causes.",
            turns=limits["serviceops_triage"][0],
            tool_calls=limits["serviceops_triage"][1],
            steps_field="triage",
            safeguards=(
                "Everything its tools show the model has personal data redacted.",
                "An incident's own confirmed cause is never shown to it.",
                "No tool can start an automation; a person confirms every cause.",
            ),
            measured="manage.py triage_pack (paid) asks about incidents with confirmed "
            "causes with tools and without, before it is switched on in production.",
            one_call="Asks once over its evidence pack.",
        ),
    )


def agents():
    return _agents()


def get(key):
    for agent in _agents():
        if agent.key == key:
            return agent
    return None


def tools(agent):
    """The tools the harness hands this agent: the registry's own list for its purpose."""
    from .agent_runtime.registry import for_scope

    return for_scope(SimpleNamespace(purpose=agent.purpose))


def status(agent, app):
    """(state, label, detail) for this agent in this application, now.

    `on` runs as an agent; `one-call` runs, but asks once with no tools; `off` does
    not run here.
    """
    from .models import AI_PURPOSES, AIConfiguration
    from .services import FEATURES, feature_enabled

    if not feature_enabled(agent.feature, app):
        name = FEATURES.get(agent.feature, (agent.feature,))[0]
        return "off", "Off", f"{name} is switched off for this application."
    config = AIConfiguration.objects.filter(application=app, purpose=agent.purpose).first()
    task = dict(AI_PURPOSES).get(agent.purpose, agent.purpose)
    if config is None or not config.enabled:
        return "off", "Off", f"{task} is off in this application's AI settings."
    if agent.switch and not getattr(settings, agent.switch, False):
        setting = agent.switch.lower()
        return (
            "one-call",
            "One call",
            f"Its tools are off in this deployment, so it asks once. "
            f"{agent.one_call} An operator turns them on with {setting} = true.",
        )
    return "on", "On", f"Runs as an agent with {task}'s model, {config.model}."


def activity(agent, app, days=ACTIVITY_DAYS):
    """Counts of what this agent did here lately - runs, steps, by tool and outcome."""
    from .models import AgentStep

    since = timezone.now() - timedelta(days=days)
    steps = AgentStep.objects.filter(
        application=app, purpose=agent.purpose, created_at__gte=since
    ).exclude(**{f"{agent.steps_field}__isnull": True})
    rows = list(steps.values_list(f"{agent.steps_field}_id", "tool", "outcome"))
    return {
        "days": days,
        "runs": len({row[0] for row in rows}),
        "steps": len(rows),
        "tools": Counter(row[1] for row in rows).most_common(),
        "outcomes": Counter(row[2] for row in rows).most_common(),
    }


def described(agent, app):
    state, label, detail = status(agent, app)
    return {
        "agent": agent,
        "state": state,
        "label": label,
        "detail": detail,
        "tools": tools(agent),
        "budget": agent.turns + (1 if agent.kept_back else 0),
    }


@login_required
@require_GET
def agents_view(request, pk):
    """Every agent and how it stands here. Anyone who can open the application."""
    from .policy import application_for

    app, grant = application_for(request.user, pk)
    rows = [described(agent, app) for agent in _agents()]
    return render(request, "agents.html", {"application": app, "grant": grant, "rows": rows})


@login_required
@require_GET
def agent_view(request, pk, key):
    """One agent: what it does, where it runs, its tools, limits, safeguards and
    recent activity. Opened as a popup by its badge; a page of its own without
    JavaScript."""
    from .policy import application_for

    app, grant = application_for(request.user, pk)
    agent = get(key)
    if agent is None:
        raise Http404
    return render(
        request,
        "agent.html",
        {
            "application": app,
            "grant": grant,
            **described(agent, app),
            "safeguards": (*agent.safeguards, *SHARED_SAFEGUARDS),
            "activity": activity(agent, app),
        },
    )
