"""Code Factory: plans, runs, their phases, changes and events."""

import uuid

from django.conf import settings
from django.db import models

from .accounts import Application


class ChangePlan(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(Application, on_delete=models.PROTECT)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    title = models.CharField(max_length=200)
    proposal = models.TextField(max_length=20000)
    validation = models.TextField(max_length=10000)
    sources = models.JSONField(default=list)
    digest = models.CharField(max_length=64)
    status = models.CharField(
        max_length=16,
        default="pending",
        choices=[
            ("pending", "Awaiting approval"),
            ("approved", "Approved"),
            ("rejected", "Rejected"),
        ],
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="reviews"
    )
    review_note = models.CharField(max_length=2000, blank=True)
    reviewed_at = models.DateTimeField(null=True)
    #: Which published graph version the analysis was grounded in, so a plan
    #: stays reproducible after a later graph is published.
    graph_version = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]


#: What an analysis item is about. A ticket states some of what it wants; the
#: rest is found by comparing the ticket against what the graph says the system
#: actually does, and against the non-functional rubric.
ITEM_CATEGORIES = [
    ("stated", "Stated in the ticket"),
    ("functional", "Functional gap"),
    ("non_functional", "Non-functional gap"),
]

#: Non-functional headings the analysis reasons in. docs/index.html (How we build)
#: lists the same four, so a reader and the analysis use the same terms.
RUBRIC = [
    ("security", "Security and privacy"),
    ("availability", "Availability and operability"),
    ("performance", "Performance and resource limits"),
    ("auditability", "Auditability and correctness"),
]

PHASES = [
    ("triage", "Triage"),
    ("analysis", "Analysis"),
    ("design", "Design"),
    # The implementation half is a chain of agents rather than one call. Each is
    # its own model call with its own instructions, budget and receipt, so a run
    # can say which of them produced what and which of them refused.
    ("work_order", "Work order"),
    ("implementation", "Implementation"),
    ("tests", "Test author"),
    ("review", "Change review"),
    ("verification", "Verification"),
    ("delivery", "Delivery"),
]


class FactoryRun(models.Model):
    """One pass of the Code Factory pipeline over one ticket.

    The run and its phases are the record of what happened: which ticket, which
    graph version answered, which agent ran, what it cost and what it produced.
    A phase cannot start unless the one before it succeeded, so the record is the
    state machine rather than a commentary on one.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    #: A short number people can say out loud. The id is a UUID because it
    #: addresses one run across a platform; "run 7" is how somebody refers to it
    #: in a sentence, and it only has to be unique within its application.
    number = models.PositiveIntegerField(default=0)
    application = models.ForeignKey(Application, on_delete=models.PROTECT)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    connector = models.ForeignKey("Connector", null=True, blank=True, on_delete=models.SET_NULL)
    # The ticket, copied rather than referenced: a run has to stay readable after
    # the source entry is superseded by a later import.
    ticket_external_id = models.CharField(max_length=120, blank=True)
    ticket_title = models.CharField(max_length=300, blank=True)
    ticket_url = models.URLField(max_length=1000, blank=True)
    ticket_digest = models.CharField(max_length=64, blank=True)
    #: The text of a ticket typed in on the Code Factory screen. Empty for an
    #: imported ticket, whose body is read from its knowledge entry instead.
    #: Held here, not as knowledge, so a hand-written ticket never reaches
    #: Sources or a graph build.
    ticket_body = models.TextField(blank=True)
    #: Which published graph answered. Null means none was available.
    graph_version = models.PositiveIntegerField(null=True, blank=True)
    code_snapshot = models.ForeignKey(
        "CodeSnapshot", null=True, blank=True, on_delete=models.SET_NULL
    )
    #: A repository named in the ticket. Never acted on without confirmation:
    #: ticket text is data, and whoever can file a ticket must not be able to
    #: choose where this platform writes.
    proposed_repository = models.CharField(max_length=200, blank=True)
    repository_confirmed = models.BooleanField(default=False)
    #: The branch a delivery targets. Confirmed alongside the repository.
    base_branch = models.CharField(max_length=200, default="main")
    pull_request_url = models.URLField(max_length=1000, blank=True)
    #: The server folder a change was written into, for a repository Code Graph
    #: reads from a folder rather than GitHub. The local counterpart of
    #: `pull_request_url`: set once, by `code_factory_local.apply_local`.
    delivered_to = models.CharField(max_length=500, blank=True)
    #: The branch delivery created, kept so the checks stage knows what to ask
    #: GitHub about without re-reading a phase's output.
    branch = models.CharField(max_length=200, blank=True)
    #: What the repository's own CI made of the change, as GitHub last reported
    #: it. This platform never runs the code it writes; the repository's checks
    #: do, in GitHub's sandbox, exactly as for any other contributor. Stored
    #: rather than fetched per render so the page costs nothing to look at.
    checks = models.JSONField(default=list, blank=True)
    checks_read_at = models.DateTimeField(null=True, blank=True)
    #: Who asked for the background step this run is in. The worker runs as
    #: them and re-checks their grant, so revoking it stops the work with no
    #: code that knows about revocation - the property connectors already have.
    acting_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="factory_actions",
    )
    plan = models.ForeignKey(
        "ChangePlan", null=True, blank=True, on_delete=models.SET_NULL, related_name="runs"
    )
    status = models.CharField(
        max_length=20,
        default="pending",
        choices=[
            ("pending", "Queued"),
            ("running", "Running"),
            ("awaiting_review", "Awaiting review"),
            ("prepare_queued", "Queued to write the change"),
            ("preparing", "Writing the change"),
            ("prepared", "Ready to open a pull request"),
            ("delivering", "Opening the pull request"),
            ("delivered", "Pull request opened"),
            ("complete", "Complete"),
            ("failed", "Failed"),
        ],
    )
    error = models.TextField(blank=True)
    #: What was decided about bringing this application's own knowledge up to
    #: date once the change existed. Recorded rather than inferred: "nobody has
    #: been asked yet" and "somebody looked at it and said no" are different
    #: states, and a screen that showed them the same way would keep asking a
    #: question that has already been answered.
    refresh_choice = models.CharField(
        max_length=16,
        blank=True,
        default="",
        choices=[("queued", "Refresh queued"), ("declined", "Left as they are")],
    )
    #: Which of REFRESH_TARGETS were chosen. A subset is a real answer - the
    #: code moved and the documents did not, say - so it is kept rather than
    #: flattened to a yes.
    refresh_scope = models.JSONField(default=list, blank=True)
    refresh_at = models.DateTimeField(null=True, blank=True)
    refresh_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="factory_refreshes",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["application", "-created_at"])]
        constraints = [
            models.UniqueConstraint(fields=["application", "number"], name="unique_run_number")
        ]

    def __str__(self):
        return f"Run {self.number}"

    #: The stages of the pipeline, in order, as the screens name them.
    STAGES = (
        (1, "Pre-checks"),
        (2, "Analysis"),
        (3, "Gaps"),
        (4, "Agents"),
        (5, "Pull request"),
        (6, "Tests"),
        # The change is written and tested, and this application's own knowledge
        # is now describing the code as it was. Asked rather than done: every one
        # of these supersedes something a past run reasoned about, and two of
        # them can cost money.
        (7, "Refresh"),
    )

    @property
    def stages(self):
        """Where this run has got to, for any screen that shows a run.

        Computed here rather than in a template because three screens show it -
        the Code Factory home page, the run history and the run itself - and a
        run that reads as "awaiting review" in one place and "running" in
        another is the drift this exists to prevent.
        """
        plan = self.plan
        failed = self.status == "failed"
        analysed = plan is not None
        decided = bool(plan and plan.status in {"approved", "rejected"})
        approved = bool(plan and plan.status == "approved")
        delivered = self.delivered
        written = delivered or self.status == "prepared"
        state = {
            1: "ok" if self.status != "pending" else "pending",
            2: "ok" if analysed else ("failed" if failed else "running"),
            3: "ok" if decided else ("current" if analysed else "pending"),
            4: "ok"
            if written
            else (
                "running"
                if self.status in {"prepare_queued", "preparing"}
                else ("current" if approved else "pending")
            ),
            5: "ok"
            if delivered
            else (
                "running" if self.status == "delivering" else ("current" if written else "pending")
            ),
            # The repository's own CI, which this platform reads and never runs.
            6: self.checks_state,
            # Worth asking once there is a change to have gone stale against,
            # and not while its tests are still deciding whether it survives.
            # A repository with no CI has nothing to wait for, so "pending"
            # checks are not a reason to hold the question back.
            7: "ok"
            if self.refresh_choice
            else (
                "current" if delivered and self.checks_state != "running" else "pending"
            ),
        }
        # A failed run stops where it stopped: everything after the failure is
        # not "pending", it is never going to happen unless somebody acts.
        if failed:
            for number in sorted(state):
                if state[number] not in {"ok"}:
                    state[number] = "failed"
                    break
        # A folder run writes to a folder, not a pull request. Told apart without
        # a query: a GitHub repository is always owner/name, a folder never is.
        local = bool(self.delivered_to) or bool(
            self.proposed_repository and "/" not in self.proposed_repository
        )
        return [
            {
                "number": number,
                "label": "Write to folder" if local and number == 5 else label,
                "state": state[number],
            }
            for number, label in self.STAGES
        ]

    @property
    def delivered(self):
        """Whether the change has left this platform: a pull request, or a folder."""
        return bool(self.pull_request_url or self.delivered_to)

    def get_status_display(self):
        # "Pull request opened" is untrue of a change written into a folder.
        if self.status == "delivered" and self.delivered_to:
            return "Written to folder"
        return self._get_FIELD_display(self._meta.get_field("status"))

    def get_checks_label(self):
        """The checks as one line, for a collapsed stage."""
        if not self.checks:
            return "no checks reported"
        done = [entry for entry in self.checks if entry.get("conclusion")]
        passed = [entry for entry in done if entry["conclusion"] == "success"]
        if len(done) < len(self.checks):
            return f"{len(done)} of {len(self.checks)} finished"
        return f"{len(passed)} of {len(self.checks)} passed"

    @property
    def checks_state(self):
        """What the repository's checks make of this branch, in one word.

        "pending" covers both "no pull request yet" and "a pull request with no
        checks": a repository without CI is not failing, it simply has nothing
        to say, and the screen says that rather than showing an empty stage.
        """
        if not self.delivered or not self.checks:
            return "pending"
        if any(entry.get("conclusion") in {"failure", "timed_out"} for entry in self.checks):
            return "failed"
        if any(not entry.get("conclusion") for entry in self.checks):
            return "running"
        return "ok"

    @property
    def latest_step(self):
        """The last thing this run said, for a list with no room for the rest.

        Reads the prefetched events rather than asking the database again, so a
        list of runs stays one query for all of their narration.
        """
        events = list(self.events.all())
        return events[-1] if events else None

    @property
    def in_flight(self):
        """Still moving, so a page showing it should keep refreshing itself.

        Not called `active`: on a KnowledgeEntry that word means "not superseded",
        and a finished run is not a retired one.
        """
        return self.status in {
            "pending",
            "running",
            "prepare_queued",
            "preparing",
            "delivering",
        }


class RunPhase(models.Model):
    """One SDLC phase of one run, with everything needed to audit it."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(FactoryRun, on_delete=models.CASCADE, related_name="phases")
    name = models.CharField(max_length=20, choices=PHASES)
    sequence = models.PositiveIntegerField()
    agent = models.CharField(max_length=60, blank=True)
    status = models.CharField(
        max_length=12,
        default="pending",
        choices=[
            ("pending", "Pending"),
            ("running", "Running"),
            ("ok", "Succeeded"),
            ("failed", "Failed"),
            ("skipped", "Skipped"),
        ],
    )
    provider = models.CharField(max_length=12, blank=True)
    model = models.CharField(max_length=160, blank=True)
    # Null rather than zero until a receipt exists: a request whose usage could
    # not be read was still billed, and recording it as free would hide that.
    prompt_tokens = models.PositiveIntegerField(null=True, blank=True)
    completion_tokens = models.PositiveIntegerField(null=True, blank=True)
    citations_verified = models.PositiveIntegerField(default=0)
    citations_rejected = models.PositiveIntegerField(default=0)
    #: Digests of what went in and what came out, so an identical rerun is
    #: recognisable as one without storing prompts or model output.
    input_digest = models.CharField(max_length=64, blank=True)
    output_digest = models.CharField(max_length=64, blank=True)
    output = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["run", "sequence"]
        constraints = [models.UniqueConstraint(fields=["run", "name"], name="unique_run_phase")]


class ProposedChange(models.Model):
    """One file as it will be written, held between two decisions.

    Implementation and the pull request used to be one act, so the contents
    existed only inside that call and nobody saw what was about to be written
    until it had been. Splitting them puts a person between the two, and the
    contents have to survive the wait - which is what this is.

    `base_sha` is the blob the file was read at, or empty for a path that is not
    in the repository. It is what verification re-checks and what the commit is
    guarded on, so a file that moved underneath us is refused rather than
    overwritten.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(FactoryRun, on_delete=models.CASCADE, related_name="changes")
    path = models.CharField(max_length=400)
    content = models.TextField()
    #: Empty means the path was absent when it was read, so this creates it.
    base_sha = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["run", "path"]
        constraints = [models.UniqueConstraint(fields=["run", "path"], name="unique_run_change")]

    @property
    def creates(self):
        return not self.base_sha

    @property
    def size(self):
        return len(self.content.encode("utf-8"))


class RunEvent(models.Model):
    """One line of what a run is doing, in the words someone watching would use.

    A phase records what an agent produced; this records the steps between --
    which graph was consulted, which snapshot was pinned, what was checked before
    a model was called at all. The two are not the same thing: a phase row says
    "analysis, ok, 4,100 tokens", which is the receipt, not the story.

    Written by whichever thread owns the run at the time -- the worker for Build
    A, the request for delivery -- and never by both at once, because a run is in
    exactly one of those states. `sequence` is therefore allocated as one more
    than the highest so far without locking; `at` breaks any tie it loses.

    Kept after the run finishes, so a run read next month reads the way it read
    live. That is the whole reason this is a table and not a log line.
    """

    LEVELS = [
        ("step", "Step"),
        ("check", "Check"),
        ("result", "Result"),
        ("problem", "Problem"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(FactoryRun, on_delete=models.CASCADE, related_name="events")
    sequence = models.PositiveIntegerField()
    #: The phase this belongs to, or "" for the run itself.
    phase = models.CharField(max_length=20, blank=True)
    level = models.CharField(max_length=8, choices=LEVELS, default="step")
    message = models.CharField(max_length=300)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["run", "sequence", "at"]
        indexes = [models.Index(fields=["run", "sequence"])]


class PlanItem(models.Model):
    """One gap and the fix proposed for it.

    A plan used to be a single block of prose, which could not say "here are six
    things, each with its own evidence and its own change". Each item carries the
    graph citations that justify it, so a reviewer can check the claim rather
    than take it.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    plan = models.ForeignKey("ChangePlan", on_delete=models.CASCADE, related_name="items")
    sequence = models.PositiveIntegerField()
    category = models.CharField(max_length=16, choices=ITEM_CATEGORIES)
    rubric = models.CharField(max_length=20, choices=RUBRIC, blank=True)
    title = models.CharField(max_length=300)
    explanation = models.TextField(max_length=4000)
    change_summary = models.TextField(max_length=4000)
    targets = models.JSONField(default=list, blank=True)
    #: Verified graph citations: active source, matching digest, exact quote.
    citations = models.JSONField(default=list, blank=True)
    severity = models.CharField(
        max_length=8,
        default="medium",
        choices=[("low", "Low"), ("medium", "Medium"), ("high", "High")],
    )
    status = models.CharField(
        max_length=10,
        default="proposed",
        choices=[
            ("proposed", "Proposed"),
            ("accepted", "Accepted"),
            ("rejected", "Rejected"),
        ],
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["plan", "sequence"]
