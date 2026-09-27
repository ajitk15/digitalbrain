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

from django.db.models import Count, Exists, OuterRef, Q
from django.urls import reverse
from django.utils import timezone

from .models import ApplicationGrant, Connector, FactoryRun, TriageHypothesis, TriageRun
from .services import AREA_LABELS, feature_enabled, purposes

#: How long a failed run stays on the list. After that it is history, and the
#: run list is where history lives.
FAILED_RUN_WINDOW = timedelta(days=14)
#: The Overview shows this many; the rest are on the full list, one click away.
MAX_ITEMS = 8

#: What kind of thing is waiting, in the order the full list offers them as
#: filters. A kind is a fixed label, never data.
KINDS = {
    "review": "Plans to review",
    "delivery": "Runs to carry on",
    "failure": "Failures",
    "assessment": "Ideas to assess",
    "setup": "Setup to finish",
}


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
    #: Which of `KINDS` this is, for filtering the full list.
    kind: str = "review"


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
    """What is waiting on this person, most pressing first.

    All of it: the caller shows the first MAX_ITEMS and says how many there are,
    so a long list is never cut short without saying so. Each query is bounded in
    the database - live states, a recent window, the latest run per incident -
    rather than loading history and discarding it here.
    """
    from .factory_views import self_approval_allowed

    apps = list(apps)
    if not apps:
        return []
    grants = {
        grant.application_id: grant
        for grant in ApplicationGrant.objects.filter(user=user, application__in=apps)
    }
    by_id = {app.pk: app for app in apps}
    found = []

    recent = timezone.now() - FAILED_RUN_WINDOW
    factory_apps = [
        app for app in apps if app.pk in grants and feature_enabled("code_factory", app)
    ]
    runs = (
        FactoryRun.objects.filter(application__in=factory_apps)
        .filter(
            Q(status__in=["awaiting_review", "prepared"])
            | Q(status="failed", finished_at__gte=recent)
        )
        .select_related("plan")
        .order_by("-created_at")
    )
    for run in runs:
        app = by_id[run.application_id]
        grant = grants[app.pk]
        url = reverse("run-detail", args=[app.pk, run.pk])
        name = f"Run {run.number} · {run.ticket_external_id or run.ticket_title[:40]}"
        mine = user.pk in {run.requested_by_id, run.acting_user_id}
        plan_status = run.plan.status if run.plan else ""
        # The same rule the approval screen enforces: an author may not review
        # their own plan unless this deployment allows self-approval. The
        # prompt once invited authors to a review the screen then refused.
        may_review = grant.can_approve and (
            run.plan is None or run.plan.author_id != user.pk or self_approval_allowed()
        )
        if run.status == "awaiting_review" and plan_status == "pending" and may_review:
            found.append(
                Item(
                    app,
                    f"{name}: a plan is waiting for your review",
                    url,
                    "code",
                    "Review the plan",
                    kind="review",
                )
            )
        elif (
            run.status == "awaiting_review"
            and plan_status == "approved"
            and (mine or grant.can_approve)
        ):
            found.append(
                Item(
                    app,
                    f"{name}: approved, waiting for its next step",
                    url,
                    "code",
                    "Carry on",
                    kind="delivery",
                )
            )
        elif run.status == "prepared" and (mine or grant.can_approve):
            found.append(
                Item(
                    app,
                    f"{name}: ready to open a pull request",
                    url,
                    "github",
                    "Open it",
                    kind="delivery",
                )
            )
        elif run.status == "failed" and mine:
            found.append(
                Item(
                    app, f"{name}: failed", url, "error", "See why", tone="problem", kind="failure"
                )
            )

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
                    kind="failure",
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
    # on a run that a newer one replaced are not waiting on anyone. Counted in
    # the database, so the cost does not grow with every run ever made.
    newer = TriageRun.objects.filter(
        application_id=OuterRef("application_id"),
        incident_id=OuterRef("incident_id"),
        number__gt=OuterRef("number"),
    )
    unassessed = TriageHypothesis.objects.filter(run=OuterRef("pk"), verdicts__isnull=True)
    waiting = dict(
        TriageRun.objects.filter(application__in=assessors, status="completed")
        .filter(~Exists(newer), Exists(unassessed))
        .order_by()
        .values("application_id")
        .annotate(count=Count("pk"))
        .values_list("application_id", "count")
    )
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
                kind="assessment",
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
                        kind="setup",
                    )
                )

    # Failures first: something broke. Then decisions waiting on a person.
    found.sort(key=lambda item: item.tone != "problem")
    return found
