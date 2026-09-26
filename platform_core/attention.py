"""What the Overview shows: where to start in each application, and what needs you.

The Overview used to count organizations and applications and link each one to
its knowledge. Neither tells somebody arriving what to do. This answers two
questions instead, for the applications the person holds a grant on:

* what can I do here - one action per enabled part of the product;
* what is waiting on me - a plan to review, a run to carry on, a failed run or
  import, ideas to assess, setup to finish.

Everything is scoped by the caller's own grants. An item only appears for
someone who can act on it: plans for approvers, imports for owners, assessments
for anyone but a viewer. Read-only: nothing here writes.
"""

from dataclasses import dataclass
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from .models import ApplicationGrant, Connector, FactoryRun, TriageHypothesis, TriageRun
from .services import AREA_LABELS, feature_enabled, purposes

#: How long a failed run stays on the list. After that it is history, and the
#: run list is where history lives.
FAILED_RUN_WINDOW = timedelta(days=14)
#: The list is a prompt, not a report: the rest is one click away on each screen.
MAX_ITEMS = 8


@dataclass(frozen=True)
class Action:
    label: str
    url: str
    icon: str


@dataclass(frozen=True)
class Item:
    application: object
    text: str
    url: str
    icon: str
    action: str
    #: "attention" for something waiting on a decision, "problem" for a failure.
    tone: str = "attention"


def actions(app):
    """Where to start, one per enabled part of the product, most specific first."""
    found = []
    if feature_enabled("code_factory", app):
        found.append(Action("Analyze a ticket", reverse("plans", args=[app.pk]), "code"))
    if feature_enabled("service_ops", app) and feature_enabled("knowledge", app):
        found.append(Action("Triage an incident", reverse("serviceops", args=[app.pk]), "pulse"))
    if feature_enabled("chat", app):
        found.append(Action("Ask a question", reverse("chat", args=[app.pk]), "chat"))
    if feature_enabled("knowledge", app):
        found.append(Action("Explore knowledge", reverse("graph", args=[app.pk]), "knowledge"))
    return found


def cards(apps):
    return [
        {
            "application": app,
            "purposes": [AREA_LABELS[purpose] for purpose in purposes(app)],
            "actions": actions(app),
        }
        for app in apps
    ]


def items(user, apps):
    """What is waiting on this person, most pressing first, at most MAX_ITEMS."""
    apps = list(apps)
    if not apps:
        return []
    grants = {
        grant.application_id: grant
        for grant in ApplicationGrant.objects.filter(user=user, application__in=apps)
    }
    by_id = {app.pk: app for app in apps}
    found = []

    runs = (
        FactoryRun.objects.filter(application__in=apps)
        .filter(status__in=["awaiting_review", "prepared", "failed"])
        .select_related("plan")
        .order_by("-created_at")
    )
    recent = timezone.now() - FAILED_RUN_WINDOW
    for run in runs:
        app = by_id[run.application_id]
        grant = grants.get(app.pk)
        if grant is None or not feature_enabled("code_factory", app):
            continue
        url = reverse("run-detail", args=[app.pk, run.pk])
        name = f"Run {run.number} · {run.ticket_external_id or run.ticket_title[:40]}"
        mine = user.pk in {run.requested_by_id, run.acting_user_id}
        plan_status = run.plan.status if run.plan else ""
        if run.status == "awaiting_review" and plan_status == "pending" and grant.can_approve:
            found.append(
                Item(
                    app,
                    f"{name}: a plan is waiting for your review",
                    url,
                    "code",
                    "Review the plan",
                )
            )
        elif (
            run.status == "awaiting_review"
            and plan_status == "approved"
            and (mine or grant.can_approve)
        ):
            found.append(
                Item(app, f"{name}: approved, waiting for its next step", url, "code", "Carry on")
            )
        elif run.status == "prepared" and (mine or grant.can_approve):
            found.append(
                Item(app, f"{name}: ready to open a pull request", url, "github", "Open it")
            )
        elif run.status == "failed" and mine and run.finished_at and run.finished_at >= recent:
            found.append(Item(app, f"{name}: failed", url, "error", "See why", tone="problem"))

    owned = [app for app in apps if getattr(grants.get(app.pk), "role", "") == "owner"]
    for connector in Connector.objects.filter(
        application__in=owned, enabled=True, last_status="failed"
    ).order_by("name"):
        app = by_id[connector.application_id]
        if feature_enabled("connectors", app):
            found.append(
                Item(
                    app,
                    f"{connector.name} import failed",
                    reverse("connectors", args=[app.pk]),
                    "plug",
                    "See why",
                    tone="problem",
                )
            )

    assessors = [
        app
        for app in apps
        if getattr(grants.get(app.pk), "role", "viewer") != "viewer"
        and feature_enabled("service_ops", app)
        and feature_enabled("knowledge", app)
    ]
    # Each incident's latest run only, as the ServiceOps list counts them: ideas
    # on a run that a newer one replaced are not waiting on anyone.
    latest = {}
    for run_id, app_id, incident_id, status in (
        TriageRun.objects.filter(application__in=assessors)
        .order_by("-number")
        .values_list("pk", "application_id", "incident_id", "status")
    ):
        latest.setdefault(incident_id, (run_id, app_id, status))
    open_runs = {
        run_id: app_id for run_id, app_id, status in latest.values() if status == "completed"
    }
    waiting = {}
    for run_id in (
        TriageHypothesis.objects.filter(run__in=list(open_runs), verdicts__isnull=True)
        # Without clearing the model's ordering by rank, distinct() counts
        # each idea rather than each run: two incidents read as five.
        .order_by()
        .values_list("run_id", flat=True)
        .distinct()
    ):
        waiting[open_runs[run_id]] = waiting.get(open_runs[run_id], 0) + 1
    for app_id, count in waiting.items():
        app = by_id[app_id]
        found.append(
            Item(
                app,
                f"{count} triaged incident{'s' if count != 1 else ''} "
                "with ideas nobody has assessed",
                reverse("serviceops", args=[app.pk]) + "?show=unassessed",
                "pulse",
                "Assess them",
            )
        )

    for app in apps:
        if (
            app.setup_completed_at is None
            and app.pk in grants
            and feature_enabled("knowledge", app)
        ):
            from .readiness import setup

            state = setup(app)
            if not state.analysis_ready:
                found.append(
                    Item(
                        app,
                        f"Setup: {state.done} of {state.total} done"
                        + (f" - next, {state.next_step.label.lower()}" if state.next_step else ""),
                        reverse("onboarding", args=[app.pk]),
                        "empty",
                        "Continue setup",
                    )
                )

    # Failures first: something broke. Then decisions waiting on a person.
    found.sort(key=lambda item: item.tone != "problem")
    return found[:MAX_ITEMS]
