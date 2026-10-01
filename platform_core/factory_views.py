"""Code Factory's screens: plans, runs, each agent's report, and review.

Split out of `workbench` along a line it already had: the chat and knowledge
views there, the Code Factory views here. Access goes through the same
`workbench.access`, so nothing about who may see or approve what moved.
"""

import json
from types import SimpleNamespace

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import models, transaction
from django.http import (
    Http404,
    HttpResponse,
    HttpResponseNotAllowed,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .model_catalog import MODEL_CHOICES
from .models import (
    AIConfiguration,
    ChangePlan,
    KnowledgeEntry,
)
from .services import audit
from .workbench import access, published_graph_version


def plans(request, pk):
    """Start a run, and see the ones this application has made.

    This used to carry a second way to make a change plan - a form somebody
    filled in, with an AI-drafted first attempt - and a list of every plan an
    application held. Neither survived contact with the pipeline: a plan is
    what a run produces, it is read on the run that produced it, and the list
    was a worse route to the same thing with no ticket and no stage on it.

    The drafting POST paths went with the list. They had no page posting to
    them, which is code claiming a capability the product does not offer.
    """
    from .code_factory import NO_GRAPH, ticket_choices
    from .models import Connector, FactoryRun

    app, grant = access(request.user, pk, "code_factory")
    connectors = Connector.objects.filter(application=app, enabled=True)
    if request.method == "POST" and request.POST.get("action") == "analyse":
        from .code_factory import start_run

        access(request.user, pk, "code_factory", write=True)
        entry = get_object_or_404(
            KnowledgeEntry, pk=request.POST.get("ticket"), application=app, active=True
        )
        connector = Connector.objects.filter(
            pk=request.POST.get("connector"), application=app
        ).first()
        try:
            run = start_run(request.user, pk, entry, connector=connector)
        except ValidationError as error:
            # A run with nothing to cite is refused rather than queued, so this
            # is an answer to give the person now, not a 500.
            messages.error(request, " ".join(error.messages))
            return redirect("plans", pk=pk)
        messages.success(
            request,
            f"Queued analysis of {entry.title}. Its stages appear below as it runs.",
        )
        return redirect("run-detail", pk=pk, run_id=run.pk)
    if request.method == "POST" and request.POST.get("action") == "manual":
        return ticket_new(request, pk)
    if request.method == "POST":
        return HttpResponseNotAllowed(["GET"])
    runs_shown = list(
        FactoryRun.objects.filter(application=app).prefetch_related("phases", "events")[:5]
    )
    return render(
        request,
        "plans.html",
        {
            "application": app,
            "grant": grant,
            # Analysis is refused without one, so the form is not offered either:
            # a button that can only fail is worse than a sentence saying why.
            "published_graph": published_graph_version(app.pk),
            "unblock": dict(
                zip(
                    ("title", "detail", "url", "button", "modal"),
                    graph_unblock(app, grant),
                    strict=True,
                )
            )
            if not published_graph_version(app.pk)
            else None,
            # The same words start_run and execute refuse with (CLAUDE.md: NO_GRAPH).
            "no_graph": NO_GRAPH,
            "runs": runs_shown,
            "more_runs": FactoryRun.objects.filter(application=app).count() > len(runs_shown),
            "active": any(run.in_flight for run in runs_shown),
            "tickets": ticket_choices(app),
            "connectors": connectors,
            # The queue an analysis runs against defaults to the connector that
            # imported most recently - the one whose tickets are on screen -
            # rather than to "any". With a single connector configured, "any" and
            # "that one" are the same choice, and the template drops it.
            "default_connector": connectors.order_by(
                models.F("last_synced_at").desc(nulls_last=True), "name"
            ).first(),
        },
    )


def graph_unblock(app, grant):
    """What stands between this application and a published graph, as one step.

    Code Factory cannot analyse without a published graph, and used to say so
    in a paragraph. This names the exact next thing - publish the draft that
    exists, generate one from the sources that exist, or add sources - and
    what doing it unlocks. Returns (headline, detail, url, button, modal).
    """
    from .graphs import sources_for
    from .models import GraphRevision

    can_act = grant.role in {"owner", "contributor"}
    draft = GraphRevision.objects.filter(application=app).order_by("-number").first()
    if draft is not None:
        return (
            f"Publish graph version {draft.number}",
            "A draft is ready. Publishing it lets Code Factory analyse tickets against it; "
            "every gap a run finds will cite evidence from it.",
            reverse("graph", args=[app.pk]) + "?tab=versions",
            "Review and publish" if can_act else "",
            False,
        )
    if sources_for(app.pk).exists():
        return (
            "Generate a knowledge graph",
            "Sources are in, but no graph has been built from them yet. Generate one, "
            "publish it, and tickets can be analysed.",
            reverse("graph-generate", args=[app.pk]),
            "Generate a graph" if can_act else "",
            True,
        )
    return (
        "Add sources first",
        "Code Factory compares tickets with what this application's documents say. "
        "Add requirements and design documents, then generate and publish a graph.",
        reverse("source-add", args=[app.pk]),
        "Add sources" if can_act else "",
        True,
    )


@login_required
@require_http_methods(["GET", "POST"])
def ticket_new(request, pk):
    """Enter a ticket by hand, on a page of its own.

    Opened as a popup from Code Factory by `modal.js`, which fetches this URL
    and lifts its `<main>`; with JavaScript off the button navigates here. The
    ticket is kept on the run alone and never becomes knowledge - see
    `code_factory.start_run`.
    """
    from .code_factory import NO_GRAPH, start_run

    app, _ = access(request.user, pk, "code_factory", write=True)
    published = published_graph_version(app.pk)
    title = request.POST.get("title", "")[:300]
    description = request.POST.get("description", "")
    error = "" if published else NO_GRAPH
    if request.method == "POST":
        try:
            run = start_run(request.user, pk, title=title, body=description)
        except ValidationError as refusal:
            error = " ".join(refusal.messages)
        else:
            messages.success(
                request,
                f"Queued analysis of {run.ticket_title}. Its stages appear below as it runs.",
            )
            return redirect("run-detail", pk=pk, run_id=run.pk)
    return render(
        request,
        "ticket_new.html",
        {
            "application": app,
            "published_graph": published,
            "title": title,
            "description": description,
            "error": error,
        },
    )


@login_required
@require_http_methods(["GET"])
def plan_item(request, pk, plan_id, item_id):
    """One gap, with the evidence behind it.

    A page rather than a panel because `modal.js` opens it by fetching this URL
    and lifting its `<main>`: there is no fragment mode, and with JavaScript off
    the link simply navigates here. Read-only - choosing whether to implement it
    is a decision about the whole plan and stays on the review form, where it is
    made once.
    """
    app, grant = access(request.user, pk, "code_factory")
    plan = get_object_or_404(ChangePlan, application=app, pk=plan_id)
    item = get_object_or_404(plan.items, pk=item_id)
    return render(
        request,
        "plan_item.html",
        {"application": app, "grant": grant, "plan": plan, "item": item},
    )


@login_required
@require_http_methods(["GET"])
def runs(request, pk):
    """Every run this application has ever made, newest first.

    Separate from the Code Factory screen because that screen is for starting
    work and this one is for looking back at it: a run is kept for as long as
    the application is, and ten rows on a busy application is not a history.
    """
    from .models import FactoryRun

    app, grant = access(request.user, pk, "code_factory")
    page = Paginator(
        FactoryRun.objects.filter(application=app).prefetch_related("phases"), 20
    ).get_page(request.GET.get("page"))
    return render(
        request,
        "runs.html",
        {
            "application": app,
            "grant": grant,
            "page": page,
            "active": any(run.in_flight for run in page),
        },
    )


#: The agents of stage four, in order, with what each one is for. Named here
#: rather than read from the phase rows so a row exists before its phase does:
#: "not started yet" is the state somebody is looking at before they press the
#: button, and a missing row cannot say it.
AGENT_ROW = (
    ("work_order", "Work order", "Decides what each file must end up doing"),
    ("implementation", "Implementation", "Writes the new contents of each file"),
    ("tests", "Test author", "Writes the tests that prove the change"),
    ("review", "Change review", "Reads the finished files against the approved items"),
    ("verification", "Pre-write checks", "Re-reads every file before anything is written"),
    ("delivery", "Pull request", "Branches, commits and opens a draft"),
)


#: Each implementation agent in full, for the popup its row opens. The row keeps
#: the one-line version in AGENT_ROW. `model` says whether the agent calls one:
#: the last two are deterministic code, and naming a model for them would be
#: inventing a fact.
AGENT_EXPLAINED = {
    "work_order": {
        "does": "Reads every approved gap together and decides, for each file the "
        "change touches, what that file must end up doing and how to tell.",
        "reads": "The approved gaps, and the current contents of each file they name.",
        "produces": "One intent and a short list of checks per file, plus any approved "
        "gap that no file could satisfy.",
        "never": "Writes code. It only states what each file is for.",
        "model": True,
    },
    "implementation": {
        "does": "Writes the new contents of each file, one file at a time, against the "
        "intent the work order set for it.",
        "reads": "The work order, the approved gaps, each file as it is now, and - "
        "read-only - the code those files import and the modules the gaps name, so "
        "it calls existing code as it really is.",
        "produces": "A complete new version of every file it changes or creates.",
        "never": "Touches the repository. The files are held here until you publish.",
        "model": True,
    },
    "tests": {
        "does": "Writes the tests that prove the change. It uses the test files the "
        "approved gaps name; when they name none, it picks one per changed source file "
        "from the repository's own layout, extending an existing test where there is one.",
        "reads": "The approved gaps, the files the implementation agent wrote, any "
        "existing test file it is extending, and which test framework and CI the "
        "repository is configured with - so new tests use what is already there.",
        "produces": "New or updated test files.",
        "never": "Runs the tests. The repository's own CI does, once a pull request exists.",
        "model": True,
    },
    "review": {
        "does": "Reads each finished file against the approved gap and intent it was "
        "written for, and rejects anything that falls short or goes further.",
        "reads": "The work order and every file the other agents wrote.",
        "produces": "A keep or reject decision per file, with the reason for each rejection.",
        "never": "Edits a file. A rejected file is simply left out of the change.",
        "model": True,
    },
    "verification": {
        "does": "Re-reads every file just before anything is written, so nothing lands "
        "on top of a change somebody else made in the meantime.",
        "reads": "Each target file as it is now, compared with the version the agents read.",
        "produces": "A list of the files checked, and the new paths confirmed still absent.",
        "never": "Calls a model. These are fixed checks: paths, elisions and staleness.",
        "model": False,
    },
    "delivery": {
        "does": "Creates a branch, commits the kept files and opens a draft pull request.",
        "reads": "The files that survived review, and the confirmed repository and branch.",
        "produces": "A branch and a draft pull request. Nothing is merged.",
        "never": "Calls a model, or runs anything without your Yes on the summary.",
        "model": False,
    },
}

#: "claude-sonnet-5" as people say it, from the same catalogue AI settings offers.
MODEL_LABELS = {
    value.split(":", 1)[1]: label
    for _, group in MODEL_CHOICES
    for value, label in group
    if ":" in value
}


def agent_model(name, phase, configured):
    """The model an agent used, or would use, and which of the two this is.

    Recorded on the phase once it has run, because the setting can change
    afterwards; before that, the application's Code Factory model, since every
    model-backed agent asks through the `plan_drafting` configuration.
    """
    if not AGENT_EXPLAINED[name]["model"]:
        return {"label": "No model", "source": "deterministic", "id": ""}
    if phase is not None and phase.model:
        return {
            "label": MODEL_LABELS.get(phase.model, phase.model),
            "source": "used",
            "id": f"{phase.provider}:{phase.model}" if phase.provider else phase.model,
        }
    if configured is not None:
        return {
            "label": MODEL_LABELS.get(configured.model, configured.model),
            "source": "configured",
            "id": f"{configured.provider}:{configured.model}",
        }
    return {"label": "No model configured", "source": "missing", "id": ""}


#: What an agent's failure means, for someone who is not reading the code.
#: Keyed on the start of the recorded error, which `code_factory` writes.
FAILURE_EXPLAINED = (
    (
        "did not return usable JSON",
        "The agent must answer in a fixed, machine-readable format (JSON) so its "
        "answer can be checked before anything is used. This reply could not be read "
        "in that format, so it was set aside and nothing was written. It is usually a "
        "one-off slip by the model; rerunning normally works.",
    ),
    (
        "returned no usable file changes",
        "The agent answered, but gave back no file it had actually changed, so "
        "nothing was written. When it says why above, that reason is the model's "
        "own; the usual one is that the change depends on a file it was not shown, "
        "which a plan naming that file fixes.",
    ),
    (
        "did not return a JSON object",
        "The agent answered in JSON, but not in the shape it was asked for, so the "
        "answer was set aside and nothing was written. Rerunning normally works.",
    ),
)


def failure_explained(error):
    for fragment, explanation in FAILURE_EXPLAINED:
        if fragment in (error or ""):
            return explanation
    return ""


def analysis_view(run, phases_by_name, items):
    """The Analysis stage as results first and log second.

    It used to be the run log alone - about twenty timestamped lines where
    "Code Factory switched on" and "Gap analysis found 7 item(s)" looked the
    same. Everything here is read from what the phases already recorded, so the
    cards cannot say more than the receipt does.
    """
    from .code_factory import AGENTS, BUILD_A, BUILD_B, language_gap
    from .code_graph_analysis import describe_languages
    from .models import ITEM_CATEGORIES

    triage_phase = phases_by_name.get("triage")
    triage = (triage_phase.output or {}) if triage_phase and triage_phase.status == "ok" else {}
    analysis_phase = phases_by_name.get("analysis")
    snapshot = run.code_snapshot
    languages = getattr(snapshot, "languages", None) or []
    counts = {}
    for item in items:
        counts[item.category] = counts.get(item.category, 0) + 1
    steps = []
    for name in BUILD_A:
        phase = phases_by_name.get(name)
        steps.append(
            {
                "label": AGENTS.get(name, name),
                "status": phase.status if phase else "pending",
                "model": MODEL_LABELS.get(phase.model, phase.model) if phase else "",
                "seconds": (
                    round(phase.duration_ms / 1000, 1) if phase and phase.duration_ms else None
                ),
                "tokens": (
                    f"{phase.prompt_tokens:,} in / {phase.completion_tokens:,} out"
                    if phase and phase.prompt_tokens
                    else ""
                ),
                "error": (phase.error or "")[:200] if phase else "",
            }
        )
    # Analysis is everything before implementation was asked for. Later
    # problems - a review that failed, a CI result - belong to later stages,
    # and listing them here made the analysis read as if it had gone wrong.
    events = list(run.events.all())
    later = next(
        (
            index
            for index, event in enumerate(events)
            if event.phase in BUILD_B or event.message.startswith("Implementation requested")
        ),
        len(events),
    )
    events = events[:later]
    return {
        "triage": triage,
        "evidence": {
            "graph_version": run.graph_version,
            "verified": analysis_phase.citations_verified if analysis_phase else 0,
            "rejected": analysis_phase.citations_rejected if analysis_phase else 0,
            "snapshot": snapshot,
            "languages": describe_languages(languages) if languages else "",
        },
        "language_gap": language_gap(snapshot) if snapshot else "",
        "gaps": [
            {"key": key, "label": label, "count": counts[key]}
            for key, label in ITEM_CATEGORIES
            if counts.get(key)
        ],
        "gap_total": len(items),
        "steps": steps,
        "problems": [event for event in events if event.level == "problem"],
    }


def tests_unrun(phase):
    """The sentence for a change whose new tests nothing will run, or ""."""
    from .repo_testing import ci_summary

    if phase is None or phase.status != "ok":
        return ""
    setup = (phase.output or {}).get("setup") or {}
    if setup.get("known") and not setup.get("ci"):
        return ci_summary(setup)
    return ""


def code_factory_model(app):
    return AIConfiguration.objects.filter(
        application=app, purpose="plan_drafting", enabled=True
    ).first()


def agent_report(name, label, waiting, phase, run, configured=None):
    """One agent's row: what it is for, or what it actually did.

    A finished agent should say what happened in *this* run rather than repeat
    its job description - "Passed" is true of every successful check and tells
    a reader nothing. Each one reports from its own recorded output, which is
    the same output the phase receipt is built from.
    """
    if phase is None:
        # A run that has already been through this stage and has no row for an
        # agent never had that agent: it predates it. Saying "waiting" about
        # something that was never going to happen is worse than saying so.
        # The pull request is the one agent a prepared run has not reached yet
        # by design: it waits for a Yes on the summary, so it is waiting, not
        # missing.
        awaiting_yes = name == "delivery" and run.status == "prepared"
        past = run.status in {"prepared", "delivered", "complete"} and not awaiting_yes
        if awaiting_yes:
            waiting = "Waiting for your Yes on the summary below. Nothing is written until then."
        return {
            "name": name,
            "label": label,
            "detail": "Did not run: this run predates this agent." if past else waiting,
            "status": "absent" if past else "pending",
            "model": agent_model(name, None, configured),
        }
    output = phase.output or {}
    detail = waiting
    explanation = ""
    if phase.status == "failed":
        detail = phase.error[:200] or "Failed, with no reason recorded."
        explanation = failure_explained(phase.error)
    elif phase.status == "skipped":
        detail = output.get("reason", "Nothing for it to do.")
    elif phase.status == "running":
        detail = "Working."
    elif phase.status == "ok":
        if name == "work_order":
            order, leftover = output.get("order", {}), output.get("leftover", [])
            detail = f"Set an intent for {len(order)} file(s)"
            detail += (
                f"; {len(leftover)} approved item(s) no file could satisfy." if leftover else "."
            )
        elif name == "implementation":
            files = output.get("files", [])
            created = [item for item in files if item.get("new")]
            named = ", ".join(item.get("path", "") for item in files[:3])
            detail = f"Wrote {len(files)} file(s)"
            detail += f", {len(created)} of them new" if created else ""
            detail += f": {named}" + ("…" if len(files) > 3 else "") + "."
        elif name == "tests":
            from .repo_testing import ci_summary

            files = output.get("files", [])
            detail = f"Wrote {len(files)} test file(s): {', '.join(files[:3])}."
            setup = output.get("setup") or {}
            chosen = setup.get("python") or setup.get("javascript")
            if chosen:
                detail += f" Framework: {chosen}."
            if setup.get("known") and not setup.get("ci"):
                detail += f" {ci_summary(setup)}"
        elif name == "review":
            kept, rejected = output.get("kept", []), output.get("rejected", [])
            detail = f"Passed {len(kept)} file(s)"
            if rejected:
                first = rejected[0]
                detail += (
                    f"; rejected {len(rejected)}, including {first.get('path', '')}"
                    f" — {first.get('reason', 'no reason given')}"
                )
            orphaned = output.get("orphaned", [])
            if orphaned:
                detail += (
                    f"; dropped {len(orphaned)} test file(s) written for rejected code: "
                    + ", ".join(item.get("path", "") for item in orphaned[:3])
                )
            detail += "."
        elif name == "verification":
            checked, created = output.get("checked", []), output.get("created", [])
            detail = f"Re-read {len(checked)} file(s); none had moved"
            detail += f", and {len(created)} new path(s) were still absent." if created else "."
        elif name == "delivery":
            detail = (
                f"Committed to {output.get('branch', 'a branch')} and opened a draft pull request."
            )
    if phase.prompt_tokens:
        detail += f" ({phase.prompt_tokens:,} in / {phase.completion_tokens:,} out tokens)"
    return {
        "name": name,
        "label": label,
        "detail": detail,
        "explanation": explanation,
        "status": phase.status,
        "model": agent_model(name, phase, configured),
    }


@login_required
@require_http_methods(["GET", "POST"])
def run_agent(request, pk, run_id, name):
    """One implementation agent of one run: what it is for and what it did.

    A page rather than a panel, because `modal.js` opens it by fetching this URL
    and lifting its `<main>`; with JavaScript off the link simply navigates
    here. Read-only. Everything shown is what the phase recorded - its output
    is model text, so the template escapes it like any other.
    """
    from .code_factory_build import request_preparation
    from .models import FactoryRun

    app, grant = access(request.user, pk, "code_factory")
    run = get_object_or_404(FactoryRun.objects.select_related("plan"), pk=run_id, application=app)
    labels = {key: label for key, label, _ in AGENT_ROW}
    if name not in labels:
        raise Http404
    phase = run.phases.filter(name=name).first()
    output = (phase.output or {}) if phase else {}
    # Rerunning an agent is rerunning the implementation: the agents feed one
    # another, so one of them alone would be working from a stale input. The
    # pull request is not offered here - it writes to somebody's repository,
    # and that decision belongs on the summary where the Yes is asked for.
    can_rerun = bool(
        phase
        and phase.status == "failed"
        and run.status == "failed"
        and name != "delivery"
        and grant.can_approve
        and run.plan
        and run.plan.status == "approved"
        and not run.pull_request_url
    )
    if request.method == "POST":
        try:
            if not can_rerun:
                raise ValidationError("This agent cannot be rerun from here.")
            request_preparation(request.user, pk, run.pk)
        except ValidationError as error:
            messages.error(request, " ".join(error.messages))
        else:
            messages.success(
                request, "The implementation agents are running again. Watch them below."
            )
        return redirect(f"{reverse('run-detail', args=[pk, run.pk])}#stage-4")
    return render(
        request,
        "run_agent.html",
        {
            "application": app,
            "run": run,
            "name": name,
            "label": labels[name],
            "explained": AGENT_EXPLAINED[name],
            "phase": phase,
            "model": agent_model(name, phase, code_factory_model(app)),
            "output": output,
            "order": sorted((output.get("order") or {}).items()),
            "events": run.events.filter(phase=name).order_by("sequence"),
            "can_rerun": can_rerun,
            "explanation": failure_explained(phase.error) if phase else "",
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def run_detail(request, pk, run_id):
    """One run, step by step, while it happens and afterwards.

    The events are the narration and the phases are the receipt; both are shown
    because they answer different questions. Read-only on purpose: approving the
    plan and confirming the repository are decisions with their own gates, and
    this page links to them rather than growing a second copy of either.
    """
    from . import code_factory, code_factory_build
    from .code_factory import confirm_repository
    from .code_factory_build import (
        REFRESH_TARGETS,
        decline_refresh,
        discard,
        publish,
        refresh_after,
        request_preparation,
        write_credential,
    )
    from .models import FactoryRun
    from .readiness import setup

    app, grant = access(request.user, pk, "code_factory")
    run = get_object_or_404(
        FactoryRun.objects.select_related("plan", "code_snapshot"), pk=run_id, application=app
    )
    if request.method == "POST":
        access(request.user, pk, "code_factory", write=True)
        action = request.POST.get("action")
        try:
            if action == "review" and run.plan:
                review_plan(
                    request.user,
                    pk,
                    run.plan.pk,
                    request.POST.get("decision"),
                    request.POST.get("note", ""),
                    chosen=request.POST.getlist("item"),
                    declared=bool(request.POST.get("items_declared")),
                )
                messages.success(request, "Decision recorded.")
            elif action == "confirm-repository":
                if not grant.can_approve:
                    raise PermissionDenied
                confirm_repository(
                    run, request.POST.get("repository"), request.POST.get("base_branch")
                )
                messages.success(request, "Repository confirmed.")
            elif action == "retry-analysis":
                code_factory.retry_analysis(request.user, pk, run.pk)
                messages.success(request, "Analysis queued again. Its stages appear below.")
            elif action == "prepare":
                # Queued for the worker rather than run here: it is several
                # model calls and a series of reads, and holding the request
                # open for it would mean a blank page with nothing to report.
                request_preparation(request.user, pk, run.pk)
                messages.success(
                    request, "The implementation agents are running. Watch them below."
                )
            elif action == "publish":
                url = publish(request.user, pk, run.pk)
                messages.success(request, f"Draft pull request opened: {url}")
            elif action == "discard":
                discard(request.user, pk, run.pk)
                messages.success(request, "The prepared change was discarded. Nothing was written.")
            elif action == "refresh":
                for line in refresh_after(request.user, pk, run.pk, request.POST.getlist("target")):
                    messages.success(request, line)
            elif action == "refresh-decline":
                decline_refresh(request.user, pk, run.pk)
                messages.success(
                    request,
                    "Recorded. This application's documents and graphs are left as "
                    "they are, and this run stops asking.",
                )
            else:
                raise ValidationError("Unknown action.")
        except ValidationError as error:
            messages.error(request, " ".join(error.messages))
        return redirect("run-detail", pk=pk, run_id=run.pk)

    checks = [
        step for step in setup(app).steps if step.gate == "analysis" or step.key == "github_write"
    ]
    phases_by_name = {phase.name: phase for phase in run.phases.all()}
    items = list(run.plan.items.order_by("sequence")) if run.plan else []
    analysis = analysis_view(run, phases_by_name, items)
    if run.code_snapshot:
        # A pre-check the run itself can answer: whether the code it pinned is
        # code it can see the structure of.
        checks.append(
            SimpleNamespace(
                ok=not analysis["language_gap"],
                label="Every code language parsed"
                if not analysis["language_gap"]
                else "Some code is in a language Code Graph does not parse",
            )
        )
    configured = code_factory_model(app)
    agents = [
        agent_report(name, label, waiting, phases_by_name.get(name), run, configured)
        for name, label, waiting in AGENT_ROW
    ]
    # How long since the run last said anything. A working run that has gone
    # quiet is worth showing before the reclaimer decides it is dead: a spinner
    # that means nothing is worse than a number that means something.
    quiet_for = None
    if run.status in code_factory_build.WORKING:
        quiet_for = int(
            (timezone.now() - code_factory_build.last_sign_of_life(run)).total_seconds() // 60
        )
    plan = run.plan
    reviewable = bool(
        plan
        and plan.status == "pending"
        and grant.can_approve
        and (plan.author_id != request.user.pk or self_approval_allowed())
    )
    can_retry_analysis = bool(
        run.status == "failed" and run.plan is None and grant.role in {"owner", "contributor"}
    )
    return render(
        request,
        "run_detail.html",
        {
            "application": app,
            "grant": grant,
            "run": run,
            "events": run.events.all(),
            "phases": run.phases.all(),
            "items": plan.items.order_by("sequence") if plan else (),
            "active": run.in_flight,
            # Each stage section's state, keyed by its number as a string so the
            # template can say `stage_state.3`. Read from `run.stages`, the same
            # list the header strip and every run list render.
            "stage_state": {str(stage["number"]): stage["state"] for stage in run.stages},
            # A run that stopped before producing a plan failed in analysis, and
            # can be queued again on the same record.
            "can_retry_analysis": can_retry_analysis,
            "next": dict(
                zip(
                    ("tone", "title", "detail", "anchor", "button"),
                    next_action(run, grant, request.user, reviewable, can_retry_analysis),
                    strict=True,
                )
            ),
            "failed_agent": next((agent for agent in agents if agent["status"] == "failed"), None),
            # Section one: the gate, as green ticks rather than a page.
            "checks": checks,
            "checks_passed": sum(1 for step in checks if step.ok),
            "accepted_count": plan.items.exclude(status="rejected").count() if plan else 0,
            "agents": agents,
            "quiet_for": quiet_for,
            "stall_after": int(code_factory_build.STALL_AFTER.total_seconds() // 60),
            "reviewable": reviewable,
            "changes": run.changes.all() if run.status in {"prepared", "delivered"} else (),
            # Said on the summary, before anyone opens a pull request: tests that
            # nothing runs are not evidence, and a green PR would not say so.
            "tests_unrun": tests_unrun(phases_by_name.get("tests")),
            "analysis": analysis,
            "can_deliver": bool(
                plan
                and plan.status == "approved"
                and grant.can_approve
                and not run.pull_request_url
            ),
            "write_credential": bool(write_credential(app)),
            # Offered once the change exists and its tests have stopped moving.
            # The stage itself decides when to show; this is only the vocabulary
            # it renders, so the list and the wording live in one place.
            "refresh_targets": REFRESH_TARGETS,
            "refresh_stage": next((stage for stage in run.stages if stage["number"] == 7), None),
        },
    )


def next_action(run, grant, user, reviewable, can_retry_analysis):
    """The one thing this reader should do next on a run, or why there is none.

    Every stage already carries its own controls; a reader still had to scan
    seven of them to find the one that wanted them. This names it at the top,
    using the same conditions the stages use, so the two cannot disagree - and
    it only offers an action to someone the stage would let take it.
    Returns (tone, title, detail, anchor, button) - anchor and button empty
    when there is nothing to do but wait.
    """
    plan = run.plan
    if run.in_flight:
        return ("working", "Nothing to do yet", "The run is working. This page follows it.", "", "")
    if run.status == "failed":
        if can_retry_analysis:
            return (
                "problem",
                "Analysis failed",
                run.error or "It stopped before a plan.",
                "#stage-2",
                "Retry the analysis",
            )
        if plan and plan.status == "approved" and grant.can_approve and not run.pull_request_url:
            return (
                "problem",
                "The implementation agents failed",
                run.error or "An agent stopped without an answer.",
                "#stage-4",
                "Run the agents again",
            )
        return ("problem", "This run failed", run.error or "See the stages below.", "", "")
    if plan and plan.status == "rejected":
        return (
            "done",
            "The plan was rejected",
            "Nothing was written, and nothing more is asked of this run.",
            "",
            "",
        )
    if run.status == "awaiting_review" and plan and plan.status == "pending":
        if reviewable:
            return (
                "attention",
                "Review the plan",
                f"{plan.items.count()} item(s), each with its evidence. Untick what you do "
                "not want, then approve or reject.",
                "#stage-3",
                "Review the plan",
            )
        if grant.can_approve and plan.author_id == user.pk:
            return (
                "waiting",
                "Waiting for a second approver",
                "You wrote this plan, so somebody else approves it.",
                "",
                "",
            )
        return (
            "waiting",
            "Waiting for review",
            "Somebody with approval rights on this application decides.",
            "",
            "",
        )
    if run.status == "awaiting_review" and plan and plan.status == "approved":
        if not grant.can_approve:
            return (
                "waiting",
                "Approved - waiting for an approver to carry on",
                "Only somebody with approval rights starts the implementation.",
                "",
                "",
            )
        if not run.repository_confirmed:
            return (
                "attention",
                "Confirm the repository",
                f"{run.proposed_repository or 'The repository'} was guessed from the ticket. "
                "Confirm where a pull request would go before anything is written.",
                "#stage-4",
                "Confirm it",
            )
        return (
            "attention",
            "Run the implementation agents",
            "They write the change here, not to the repository. Several model calls.",
            "#stage-4",
            "Run the agents",
        )
    if run.status == "prepared":
        if grant.can_approve:
            return (
                "attention",
                "Decide on the pull request",
                "Read what was written, then open a draft pull request - or discard it, "
                "and nothing is written anywhere.",
                "#stage-5",
                "Read the summary",
            )
        return ("waiting", "Waiting for an approver to open the pull request", "", "", "")
    if run.status == "delivered":
        return (
            "done",
            "Pull request opened",
            "The repository's own checks run on it; their results appear below.",
            "#stage-6",
            "See the checks",
        )
    if run.status == "complete":
        return ("done", "Done", "Nothing more is asked of this run.", "", "")
    return ("waiting", run.get_status_display(), "", "", "")


def self_approval_allowed():
    """Whether one person may approve a plan they wrote.

    Off unless a deployment writes it down. Separation of duties is still the
    default and still the shape of the product: two people, one who asks and one
    who agrees. But a single-operator instance has nobody else to ask, and
    refusing this outright there left the pipeline unrunnable rather than
    strict - so it is now a deployment's decision in every mode, production
    included, instead of a development-only concession.

    What did not change is that it is never silent. `ChangePlan` records the
    approver, so a plan approved by its author says so in the audit record; and
    `review_plan` still refuses
    outright when the setting is off.
    """
    from django.conf import settings

    return bool(getattr(settings, "ALLOW_SELF_APPROVAL", True))


@transaction.atomic
def review_plan(user, app_id, plan_id, decision, note, chosen=None, declared=False):
    """Approve or reject a plan, and say which of its items are in.

    Choosing items is part of reviewing rather than a step beside it: the
    reviewer is deciding what will be built, and deciding it once - at the same
    moment, in the same submission - is what keeps "what was approved" and "what
    was delivered" the same set. Nothing can change it afterwards, because a
    plan is only reviewable while it is pending.

    `declared` distinguishes "none of them" from "this caller said nothing about
    items", for the reason the features form has the same marker: an unticked
    checkbox is simply absent from a POST, and without the marker a reviewer who
    ticked nothing and a caller that has never heard of items look identical.
    Without it every item stands, which is what happened before this existed.
    """
    app, grant = access(user, app_id, "code_factory")
    plan = get_object_or_404(ChangePlan.objects.select_for_update(), pk=plan_id, application=app)
    if not grant.can_approve or (plan.author_id == user.pk and not self_approval_allowed()):
        raise PermissionDenied("A different user with approval permission must review this plan.")
    if decision not in {"approved", "rejected"} or plan.status != "pending":
        raise ValidationError("This plan has already been reviewed or the decision is invalid.")
    if not note.strip() or len(note) > 2000:
        raise ValidationError("Enter a review note of up to 2,000 characters.")
    selected = set(chosen or ())
    if decision == "approved" and declared and not selected:
        # Approving nothing is not an approval. Saying so beats writing a plan
        # whose every item is rejected and then failing at implementation with
        # "none of the files the design named could be read".
        raise ValidationError("Choose at least one item to implement, or reject the plan instead.")
    current = {
        str(e.pk): e.digest for e in KnowledgeEntry.objects.filter(application=app, active=True)
    }
    if decision == "approved":
        # A source entry with no digest cannot be re-checked, so it cannot be
        # approved: refused in the same words rather than raising KeyError,
        # which is what a plan written before the digest was recorded did.
        if any("digest" not in source for source in plan.sources):
            raise ValidationError(
                "This plan did not record the digest of its evidence, so it "
                "cannot be verified. Submit a fresh plan."
            )
        if any(current.get(s["id"]) != s["digest"] for s in plan.sources):
            raise ValidationError("A pinned source was archived or changed. Submit a fresh plan.")
    accepted = plan.items.count()
    if declared:
        # Recorded per item rather than as a list on the plan, because every
        # consumer already asks the item: target_paths, the implementation
        # prompt and the pull request body all exclude a rejected one.
        plan.items.filter(pk__in=selected).update(status="accepted")
        plan.items.exclude(pk__in=selected).update(status="rejected")
        accepted = len(selected)
    plan.status = decision
    plan.reviewed_by = user
    plan.reviewed_at = timezone.now()
    plan.review_note = note
    plan.save(update_fields=["status", "reviewed_by", "reviewed_at", "review_note"])
    audit(
        user,
        f"plan.{decision}",
        plan.pk,
        app.product.portfolio.organization,
        details={
            "digest": plan.digest,
            "items_accepted": accepted,
            "items_total": plan.items.count(),
            # Whoever reads this later must be able to tell a reviewed change
            # from one its own author waved through.
            "self_approved": plan.author_id == user.pk,
        },
    )
    return plan


@login_required
@require_http_methods(["GET", "POST"])
def plan_detail(request, pk, plan_id):
    from .code_factory import confirm_repository
    from .code_factory_build import prepare, publish, write_credential

    app, grant = access(request.user, pk, "code_factory")
    plan = get_object_or_404(ChangePlan, application=app, pk=plan_id)
    run = plan.runs.first()
    if request.method == "POST" and request.POST.get("action") == "confirm-repository":
        # The repository was guessed - from ticket text, or from this
        # application's registry when the ticket named nothing. Confirming it
        # is a person saying "yes, that one"; whoever can file a ticket does
        # not get to decide where this platform writes.
        access(request.user, pk, "code_factory", write=True)
        if run is None or not grant.can_approve:
            raise PermissionDenied
        try:
            confirmed = confirm_repository(
                run, request.POST.get("repository"), request.POST.get("base_branch")
            )
        except ValidationError as error:
            messages.error(request, " ".join(error.messages))
            return redirect("plan-detail", pk=pk, plan_id=plan_id)
        audit(
            request.user,
            "factory.repository_confirmed",
            run.pk,
            app.product.portfolio.organization,
            details={"repository": confirmed, "base_branch": run.base_branch},
        )
        messages.success(request, "Repository confirmed. Delivery can now be requested.")
        return redirect("plan-detail", pk=pk, plan_id=plan_id)
    if request.method == "POST" and request.POST.get("action") == "deliver":
        try:
            # Kept working for anything pointing here, but it now stops at the
            # summary: the pull request is its own decision on the run page.
            prepare(request.user, pk, run.pk if run else None)
            url = publish(request.user, pk, run.pk if run else None)
        except ValidationError as error:
            messages.error(request, " ".join(error.messages))
        else:
            messages.success(request, f"Draft pull request opened: {url}")
        return redirect("plan-detail", pk=pk, plan_id=plan_id)
    if request.method == "POST":
        try:
            review_plan(
                request.user,
                pk,
                plan_id,
                request.POST.get("decision"),
                request.POST.get("note", ""),
                chosen=request.POST.getlist("item"),
                declared=bool(request.POST.get("items_declared")),
            )
        except ValidationError as error:
            messages.error(request, " ".join(error.messages))
        return redirect("plan-detail", pk=pk, plan_id=plan_id)
    if request.GET.get("download") == "1":
        if plan.status != "approved":
            raise Http404
        payload = {
            "id": str(plan.pk),
            "application": str(app.pk),
            "title": plan.title,
            "proposal": plan.proposal,
            "validation": plan.validation,
            "sources": plan.sources,
            "digest": plan.digest,
            "review_note": plan.review_note,
        }
        response = HttpResponse(json.dumps(payload, indent=2), content_type="application/json")
        response["Content-Disposition"] = f'attachment; filename="plan-{plan.pk}.json"'
        return response
    return render(
        request,
        "plan_detail.html",
        {
            "application": app,
            "plan": plan,
            "can_review": grant.can_approve
            and (plan.author_id != request.user.pk or self_approval_allowed())
            and plan.status == "pending",
            # Why not, in the words of the rule that says not. A review form
            # that is simply absent reads as a missing feature.
            "no_review_because": (
                ""
                if grant.can_approve
                and (plan.author_id != request.user.pk or self_approval_allowed())
                and plan.status == "pending"
                else f"This plan was already {plan.get_status_display().lower()}."
                if plan.status != "pending"
                else "You wrote this plan, so somebody else has to review it. "
                "Approving your own work would make the gate a formality."
                if plan.author_id == request.user.pk
                else "You do not hold approval rights on this application."
            ),
            "run": run,
            "can_deliver": bool(
                run and grant.can_approve and plan.status == "approved" and not run.pull_request_url
            ),
            "write_credential": bool(write_credential(app)),
        },
    )
