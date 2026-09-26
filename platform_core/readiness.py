"""What an application needs before Code Factory can do anything with it.

One list, three readers. The pipeline already asked these questions in three
places that could not see each other: `prevalidate` narrated some of them into a
run, `deliver` raised on others, and nothing at all told somebody setting an
application up what was still missing until they pressed a button and it failed.
Three copies of "what is required" drift, and the moment they do, a screen says
green while a run says no.

So the questions live here and the answers are computed once. The onboarding
screen renders them, `prevalidate` narrates the analysis ones into the run
record, and anything else that needs to know asks the same function.

**Two gates, deliberately not one bar.** Analysis and delivery need different
things, and an application that can analyse but not deliver is in a legitimate
state rather than a half-finished one - that is the whole shape of this product,
where describing work and doing it are separate decisions. Collapsing them into a
single percentage would say an application is 60% ready when it is in fact
completely ready to do the only thing anyone has asked it to do.

**Configuration, not run state.** Everything here is a property of the
application: a credential is mounted or it is not. Whether *this* plan was
approved, or *this* repository confirmed, belongs to the run and stays in
`code_factory.deliver` - those are decisions a person makes per change, and a
setup screen that claimed to track them would be claiming the work was finished
when it had not started.
"""

from dataclasses import dataclass

CONNECTORS = "connectors"
ANALYSIS = "analysis"
DELIVERY = "delivery"
TRIAGE = "triage"

#: (gate, heading, what it is for, purpose it belongs to or None for shared).
#: An application is shown the shared gate and the gates of the purposes it
#: serves - Engineering, Operations or both - on one page.
GATES = (
    (
        CONNECTORS,
        "Choose your connectors",
        "Which systems this application imports from. Only the ones ticked are "
        "offered on the Connectors and Credentials screens.",
        None,
    ),
    (
        ANALYSIS,
        "Engineering · Analyse a ticket",
        "What a run needs to read a ticket, compare it against the graph and "
        "propose fixes with evidence. Nothing here writes anywhere.",
        "engineering",
    ),
    (
        DELIVERY,
        "Engineering · Open a pull request",
        "What the second half additionally needs to turn an approved plan into "
        "a draft pull request. Each change still passes its own review gate.",
        "engineering",
    ),
    (
        TRIAGE,
        "Operations · Triage an incident",
        "What triage needs to find evidence for an incident - past incidents, "
        "changes and runbooks - and suggest causes that cite it. Nothing here "
        "changes any system.",
        "operations",
    ),
)
#: Drawn beside each gate's heading, matching the menu and the create form.
GATE_ICONS = {CONNECTORS: "plug", ANALYSIS: "code", DELIVERY: "github", TRIAGE: "warning"}
#: The gate that must be ready before a purpose counts as set up. Delivery is
#: deliberately not one: an application that only analyses is finished.
FIRST_GATE = {"engineering": ANALYSIS, "operations": TRIAGE}


@dataclass(frozen=True)
class Step:
    """One requirement, and what is true about it right now."""

    key: str
    gate: str
    label: str
    ok: bool
    #: What is the case, whether or not it is what we want. Always populated:
    #: "Claude claude-sonnet-5" is as useful to read as the reason it is missing.
    detail: str
    #: What to do about it, in one line. Empty when there is nothing to do.
    action: str
    #: Where to go and do it, as a url name taking the application id.
    route: str
    icon: str
    #: A run is refused without it, rather than merely going worse.
    blocking: bool = True
    #: Whether the screen that fixes this opens as a dialog over the checklist.
    #:
    #: True for the form-shaped screens, where the whole interaction is "type
    #: this, press save" and leaving the list to do it is what made onboarding
    #: feel like being bounced around. False for Knowledge, Connectors and Code
    #: Graph, which are places you go and work rather than a field you fill -
    #: lifting one of those into a dialog would be a worse version of itself.
    #:
    #: modal.js fetches the href and lifts its <main>, so the target must be a
    #: page that renders and submits on its own. Every one of these does, and
    #: with JavaScript off the link simply navigates.
    modal: bool = False


def credential_step(app, configured, gate, key):
    """Whether the provider a purpose is configured with has a credential here."""
    from django.conf import settings

    from .secrets import application_secret

    provider = configured.provider if configured else ""
    credential = bool(application_secret(app, provider)) if provider else False
    # The development concession, reported as one rather than hidden: a run does
    # work without a mounted credential here, and it works by spending whoever
    # started the server. Somebody setting up an application should be told that
    # before other people use the instance.
    host_login = provider == "claude" and not credential and settings.CLAUDE_USE_HOST_LOGIN
    return Step(
        key,
        gate,
        "Provider credential",
        credential or host_login,
        f"A {provider} credential is set for this application."
        if credential
        else "Falling back to the host Claude login. Runs spend whoever "
        "started this server, not this application."
        if host_login
        else f"No {provider or 'provider'} credential is set.",
        ""
        if credential
        else "Set a Claude credential on the Credentials screen before "
        "other people use this instance. The fallback is refused in "
        "production."
        if host_login
        else "Set the provider credential on the Credentials screen.",
        "credentials",
        "key",
        blocking=not host_login,
        modal=True,
    )


def chosen_connectors(app):
    """The connector kinds this application's owner ticked, or None if not asked yet.

    Answered means onboarding wrote a row for a kind; until then every kind is
    allowed, as a missing feature row always means enabled.
    """
    from .connector_kinds import KINDS
    from .models import ApplicationFeature
    from .services import connector_feature

    rows = dict(
        ApplicationFeature.objects.filter(
            application=app, key__in=[connector_feature(kind) for kind in KINDS]
        ).values_list("key", "enabled")
    )
    if not rows:
        return None
    return [kind for kind in KINDS if rows.get(connector_feature(kind), True)]


def connector_steps(app):
    """The one shared question: which systems this application imports from."""
    from .connector_kinds import KINDS

    chosen = chosen_connectors(app)
    names = ", ".join(KINDS[kind].label for kind in chosen or [])
    return [
        Step(
            "connectors_chosen",
            CONNECTORS,
            "Connectors chosen",
            chosen is not None,
            (f"Using {names}." if names else "No connectors: nothing is imported.")
            if chosen is not None
            else "Not chosen yet. Every kind is offered until you do.",
            "" if chosen is not None else "Tick the systems this application imports from.",
            "onboarding-connectors",
            "plug",
            modal=True,
        )
    ]


def operations_steps(app):
    """What ServiceOps triage needs, in the order somebody would set it up."""
    from .connector_kinds import KINDS
    from .graphs import published_revision
    from .models import AIConfiguration, KnowledgeEntry
    from .secrets import application_secret
    from .serviceops import incident_queryset
    from .services import feature_enabled

    found = []
    enabled = feature_enabled("service_ops", app)
    found.append(
        Step(
            "service_ops",
            TRIAGE,
            "ServiceOps switched on",
            enabled,
            "Enabled for this application." if enabled else "Not enabled.",
            "" if enabled else "Tick ServiceOps on the Features screen.",
            "application-features",
            "toggle",
            modal=True,
        )
    )
    for kind in chosen_connectors(app) or []:
        spec = KINDS[kind]
        if not spec.credential_required:
            continue
        mounted = bool(application_secret(app, kind))
        found.append(
            Step(
                f"credential_{kind}",
                TRIAGE,
                f"{spec.label} credential",
                mounted,
                f"A {spec.label} credential is set." if mounted else "Not set.",
                "" if mounted else f"Set the {spec.label} credential on the Credentials screen.",
                "credentials",
                "key",
                modal=True,
            )
        )
    incidents = incident_queryset(app).count()
    found.append(
        Step(
            "incidents",
            TRIAGE,
            "Incidents imported",
            bool(incidents),
            f"{incidents} incident(s) to triage and learn from."
            if incidents
            else "No incident has been imported.",
            ""
            if incidents
            else "Add a ServiceNow connector on the incident table and import. Resolved "
            "incidents with close notes are what triage learns from.",
            "connectors",
            "plug",
        )
    )
    changes = KnowledgeEntry.objects.filter(
        application=app, active=True, source__icontains="change_request.do"
    ).count()
    found.append(
        Step(
            "changes",
            TRIAGE,
            "Changes imported",
            bool(changes),
            f"{changes} change request(s)." if changes else "No change request has been imported.",
            ""
            if changes
            else "Add a second ServiceNow connector on the change_request table. A change "
            "on the same component shortly before an incident is often its cause.",
            "connectors",
            "plug",
            blocking=False,
        )
    )
    published = published_revision(app.pk)
    runbooks = (
        published is not None
        and KnowledgeEntry.objects.filter(
            application=app, active=True, document__isnull=False
        ).exists()
    )
    found.append(
        Step(
            "runbooks",
            TRIAGE,
            "Runbooks published",
            runbooks,
            f"Version {published.number} is published with documents."
            if runbooks
            else "No published knowledge graph with documents."
            if published is None
            else "The published graph holds no documents.",
            ""
            if runbooks
            else "Upload runbooks and design documents in Knowledge, generate a graph and "
            "publish it. Triage then reaches the passages that name an incident's component.",
            "graph",
            "graph",
            blocking=False,
        )
    )
    configured = AIConfiguration.objects.filter(
        application=app, purpose="serviceops_triage", enabled=True
    ).first()
    found.append(
        Step(
            "triage_model",
            TRIAGE,
            "Model for triage",
            configured is not None,
            f"{configured.get_provider_display()} {configured.model}."
            if configured
            else "No model is configured for ServiceOps triage.",
            "" if configured else "Choose a provider and model for ServiceOps triage in Settings.",
            "ai-settings",
            "sliders",
            modal=True,
        )
    )
    found.append(credential_step(app, configured, TRIAGE, "triage_credential"))
    return found


def all_steps(app):
    """Every step this application's onboarding shows: shared, then each purpose's."""
    from .services import purposes

    serving = purposes(app)
    found = connector_steps(app)
    if "engineering" in serving:
        found += steps(app)
    if "operations" in serving:
        found += operations_steps(app)
    return found


def steps(app):
    """Every requirement, answered for this application.

    Ordered the way somebody setting an application up would meet them, not
    grouped by which part of the code cares, because the reader is a person
    doing the work rather than a maintainer of the pipeline.
    """
    from django.conf import settings

    from .graphs import published_revision
    from .models import AIConfiguration, ApplicationGrant, CodeRepository, KnowledgeEntry
    from .secrets import application_secret
    from .services import feature_enabled

    found = []

    # ---- analysis ----
    enabled = feature_enabled("code_factory", app)
    found.append(
        Step(
            "code_factory",
            ANALYSIS,
            "Code Factory switched on",
            enabled,
            "Enabled for this application." if enabled else "Not enabled.",
            "" if enabled else "Tick Code Factory on the Features screen.",
            "application-features",
            "toggle",
            modal=True,
        )
    )

    published = published_revision(app.pk)
    found.append(
        Step(
            "graph",
            ANALYSIS,
            "Knowledge graph published",
            published is not None,
            f"Version {published.number}, published "
            f"{published.published_at:%d %b %Y}."
            if published
            else "No revision has been published.",
            ""
            if published
            else "Add sources in Knowledge, generate a graph and publish it. "
            "A run is refused without one: every item it produces has to cite "
            "evidence a reviewer can open.",
            "graph",
            "graph",
        )
    )

    configured = AIConfiguration.objects.filter(
        application=app, purpose="plan_drafting", enabled=True
    ).first()
    found.append(
        Step(
            "model",
            ANALYSIS,
            "Model for plan drafting",
            configured is not None,
            f"{configured.get_provider_display()} {configured.model}."
            if configured
            else "No model is configured for plan drafting.",
            ""
            if configured
            else "Choose a provider and model for plan drafting in Settings.",
            "ai-settings",
            "sliders",
            modal=True,
        )
    )

    found.append(credential_step(app, configured, ANALYSIS, "credential"))

    tickets = (
        KnowledgeEntry.objects.filter(application=app, active=True).exclude(source="").count()
    )
    found.append(
        Step(
            "tickets",
            ANALYSIS,
            "Tickets imported",
            bool(tickets),
            f"{tickets} imported record(s) available to analyse."
            if tickets
            else "Nothing has been imported from a connector.",
            ""
            if tickets
            else "Configure a Jira or ServiceNow connector and import. A typed "
            "note is knowledge, but it is not something anyone raised.",
            "connectors",
            "plug",
        )
    )

    # ---- delivery ----
    code_graph = feature_enabled("code_graph", app)
    indexed = (
        CodeRepository.objects.filter(
            application=app,
            provider="github",
            status__in=["ready", "partial"],
            retired_at__isnull=True,
        )
        if code_graph
        else CodeRepository.objects.none()
    )
    names = list(indexed.values_list("external_id", flat=True)[:5])
    with_snapshot = [repo for repo in indexed if repo.snapshots.exists()]
    found.append(
        Step(
            "code_graph",
            DELIVERY,
            "Code indexed",
            bool(with_snapshot),
            f"{len(with_snapshot)} repository(ies) indexed: {', '.join(names)}."
            if with_snapshot
            else "No repository has been indexed."
            if code_graph
            else "Code Graph is switched off for this application.",
            ""
            if with_snapshot
            else "Register the repository on the Code Graph screen. Without a "
            "snapshot the design names components rather than files, and "
            "implementation has nothing to open."
            if code_graph
            else "Tick Code Graph on the Features screen, then register the "
            "repository.",
            "code-graph" if code_graph else "application-features",
            "code",
        )
    )

    # Which repository a run reads is not something a ticket gets to decide, and
    # with several indexed there is nothing to fall back on: pin_repository
    # deliberately leaves the question to a person rather than guessing. Said
    # here because the consequence - a run that silently has no code context -
    # is invisible until the design comes back naming concepts.
    # Omitted entirely until something is indexed: with nothing there it is not
    # a question yet, and the step above already owns that. A checklist earns
    # trust by only showing rows that can be acted on.
    if with_snapshot:
        unambiguous = len(with_snapshot) == 1
        found.append(
            Step(
                "one_repository",
                DELIVERY,
                "One repository to read",
                unambiguous,
                f"{names[0]} is the only indexed repository, so runs pin it "
                "automatically."
                if unambiguous
                else f"{len(with_snapshot)} indexed repositories and no way to "
                "choose between them.",
                ""
                if unambiguous
                else "Remove the repositories that are not this application's "
                "code, or expect every ticket to name the one it means. A run "
                "that cannot choose reads no code at all, and its design names "
                "components instead of files.",
                "code-graph",
                "github",
            )
        )

    write = bool(application_secret(app, "github_write"))
    found.append(
        Step(
            "github_write",
            DELIVERY,
            "GitHub write credential",
            write,
            "A write-scoped GitHub credential is set."
            if write
            else "No write-scoped credential is set.",
            ""
            if write
            else "Add GitHub (write) on the Credentials screen: a token with "
            "contents and pull request write. Deliberately not the read token "
            "the connector uses.",
            "credentials",
            "key",
            modal=True,
        )
    )


    approvers = ApplicationGrant.objects.filter(application=app, can_approve=True).count()
    if settings.ALLOW_SELF_APPROVAL:
        # One person may approve their own plan here, so a second approver is
        # not something this instance needs, and asking for one is a step that
        # can never be ticked on a one-person deployment. Somebody must still
        # hold approval, or no plan is ever approved - that alone is reported.
        if not approvers:
            found.append(
                Step(
                    "approver",
                    DELIVERY,
                    "An approver",
                    False,
                    "Nobody can approve a plan.",
                    "Grant approval to a member on the People & access screen. This "
                    "instance lets one person approve the plan they asked for.",
                    "application-access",
                    "people",
                    modal=True,
                )
            )
        return found
    found.append(
        Step(
            "approver",
            DELIVERY,
            "A second approver",
            approvers > 1,
            f"{approvers} member(s) can approve."
            if approvers
            else "Nobody can approve a plan.",
            ""
            if approvers > 1
            else "Grant approval to a second member. Whoever starts a run "
            "authors its plan and cannot approve their own: with one approver, "
            "a plan they start can never be approved."
            if approvers
            else "Grant approval to at least two members on the People & access "
            "screen.",
            "application-access",
            "people",
            modal=True,
        )
    )

    return found


@dataclass(frozen=True)
class Setup:
    """Where an application has got to, for a screen or a strip to report.

    `analysis_ready` is the threshold that matters for nagging: below it the
    application cannot do the thing it exists for, and above it the rest is a
    choice. Delivery is tracked separately and never nags, because an
    application that only ever analyses tickets is finished, not half-done -
    and a checklist that can never be satisfied stops being read.
    """

    steps: tuple
    done: int
    total: int
    next_step: object
    analysis_ready: bool
    delivery_ready: bool

    @property
    def complete(self):
        return self.analysis_ready and self.delivery_ready


def setup(app):
    """Everything a caller needs to report progress, in one pass.

    `analysis_ready` means every purpose this application serves can do its
    first job - analyse a ticket, triage an incident - and the connectors are
    chosen. Delivery only counts where Engineering is switched on.
    """
    from .services import purposes

    serving = purposes(app)
    found = all_steps(app)
    first = [CONNECTORS] + [FIRST_GATE[purpose] for purpose in serving]
    return Setup(
        steps=tuple(found),
        done=sum(1 for step in found if step.ok),
        total=len(found),
        next_step=first_outstanding(found),
        analysis_ready=all(gate_ready(found, gate) for gate in first),
        delivery_ready="engineering" not in serving or gate_ready(found, DELIVERY),
    )


def record_completion(app, state):
    """Stamp the milestone the first time an application has earned it.

    Guarded on the column so two concurrent renders write it once, and done with
    `update` so it touches nothing else and fires no signal.
    """
    from django.utils import timezone

    from .models import Application

    if not state.analysis_ready or app.setup_completed_at:
        return False
    Application.objects.filter(pk=app.pk, setup_completed_at__isnull=True).update(
        setup_completed_at=timezone.now()
    )
    return True


def gate_ready(app_steps, gate):
    """Whether nothing blocking is outstanding in one gate."""
    return all(step.ok or not step.blocking for step in app_steps if step.gate == gate)


def first_outstanding(app_steps):
    """The one to do next, for a screen that should point rather than list."""
    remaining = outstanding(app_steps)
    return remaining[0] if remaining else None


def outstanding(app_steps, gate=None):
    """The steps still to do, for a count on a card or a nav badge."""
    return [
        step
        for step in app_steps
        if not step.ok and (gate is None or step.gate == gate)
    ]
