"""ServiceOps: incidents, triage, the operations graph and automations."""

import uuid

from django.conf import settings
from django.db import models

from .accounts import Application
from .knowledge import KnowledgeEntry


class IncidentProfile(models.Model):
    """Searchable, rebuildable projection of an imported incident revision."""

    entry = models.OneToOneField(KnowledgeEntry, primary_key=True, on_delete=models.CASCADE)
    application = models.ForeignKey(Application, on_delete=models.CASCADE)
    source_digest = models.CharField(max_length=64)
    number = models.CharField(max_length=80, blank=True)
    service = models.CharField(max_length=500, blank=True)
    ci = models.CharField(max_length=500, blank=True)
    priority = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=100, blank=True)
    fingerprint = models.CharField(max_length=16, blank=True)
    terms = models.JSONField(default=list)
    opened_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["application", "service", "resolved_at"]),
            models.Index(fields=["application", "fingerprint"]),
        ]


class TriageRun(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    #: "Triage run 3", unique within its application, as Code Factory numbers its
    #: runs: the id addresses a run, the number is how people refer to one.
    number = models.PositiveIntegerField(default=0)
    application = models.ForeignKey(Application, on_delete=models.CASCADE)
    incident = models.ForeignKey(KnowledgeEntry, on_delete=models.PROTECT)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    trigger = models.CharField(max_length=12, default="manual")
    status = models.CharField(max_length=20, default="evidence")
    incident_digest = models.CharField(max_length=64)
    fingerprint = models.CharField(max_length=16, blank=True)
    pack_digest = models.CharField(max_length=64)
    evidence = models.JSONField(default=list)
    band = models.CharField(max_length=20, default="insufficient")
    score_components = models.JSONField(default=dict)
    limitations = models.JSONField(default=list)
    provider = models.CharField(max_length=20, blank=True)
    model = models.CharField(max_length=160, blank=True)
    prompt_version = models.CharField(max_length=20, default="v1")
    scorer_version = models.CharField(max_length=20, default="v1")
    error = models.CharField(max_length=300, blank=True)
    #: Each step as it ran - name, state, what it found, how long it took - so a
    #: brief shows how it was reached rather than only where it ended.
    #: `serviceops_triage.STEPS` is the order; the page follows it live.
    phases = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["application", "incident", "created_at"])]


class TriageHypothesis(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(TriageRun, related_name="hypotheses", on_delete=models.CASCADE)
    rank = models.PositiveSmallIntegerField()
    #: A headline to scan before reading the statement. Blank on hypotheses made
    #: before prompt v2 asked for one; the page then shortens the statement.
    title = models.CharField(max_length=120, blank=True)
    statement = models.CharField(max_length=600)
    next_step = models.CharField(max_length=600)
    citations = models.JSONField(default=list)
    counter_evidence = models.CharField(max_length=600, blank=True)

    class Meta:
        ordering = ["rank"]
        constraints = [models.UniqueConstraint(fields=["run", "rank"], name="unique_triage_rank")]


class TriageVerdict(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    hypothesis = models.ForeignKey(
        TriageHypothesis, related_name="verdicts", on_delete=models.CASCADE
    )
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    verdict = models.CharField(
        max_length=12,
        choices=[("accepted", "Accepted"), ("rejected", "Rejected"), ("partial", "Partial")],
    )
    note = models.CharField(max_length=1000, blank=True)
    actual_cause = models.ForeignKey(
        KnowledgeEntry, null=True, blank=True, on_delete=models.PROTECT
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class OperationsGraph(models.Model):
    """One application's operations graph: when it was built, and from what.

    The operations layer is live, unlike the knowledge graph's documents: it is
    rebuilt by rules from imported incidents and changes whenever they change,
    because incidents arrive hourly and triage cannot wait for someone to
    publish. `fingerprint` is over those records' ids and digests, so a stale
    graph is noticed and rebuilt before it is read.
    """

    application = models.OneToOneField(
        Application, on_delete=models.CASCADE, related_name="operations_graph"
    )
    fingerprint = models.CharField(max_length=64, blank=True)
    built_at = models.DateTimeField(null=True, blank=True)
    node_count = models.PositiveIntegerField(default=0)
    edge_count = models.PositiveIntegerField(default=0)


class OpsNode(models.Model):
    """A thing in operations: an incident, a change, a service, a component, a symptom."""

    KINDS = [
        ("incident", "Incident"),
        ("change", "Change"),
        ("service", "Service"),
        ("component", "Component"),
        ("symptom", "Symptom"),
        ("group", "Assignment group"),
        ("passage", "Document passage"),
        ("kb", "Knowledge article"),
        ("automation", "Automation"),
    ]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(Application, on_delete=models.CASCADE)
    kind = models.CharField(max_length=12, choices=KINDS)
    #: Stable within the application: "incident:<entry id>", "component:carepath-api-green".
    key = models.CharField(max_length=300)
    label = models.CharField(max_length=300)
    #: The record behind an incident, change or passage; empty for shared things
    #: like a component, which exist because records name them.
    entry = models.ForeignKey(
        KnowledgeEntry, null=True, blank=True, on_delete=models.CASCADE, related_name="ops_nodes"
    )
    #: The facts shown on the page: number, state, priority, times, close code.
    data = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["application", "key"], name="unique_ops_node")
        ]
        indexes = [models.Index(fields=["application", "kind"])]

    def __str__(self):
        return self.label


class OpsEdge(models.Model):
    """How two things in operations relate, and why.

    Every relation but one is built by rules and replaced on every rebuild.
    `confirmed` is the exception: a person asserted it in a verdict, it survives
    rebuilds, and it goes when that verdict does.
    """

    RELATIONS = [
        ("on", "is on component"),
        ("in", "is in service"),
        ("assigned", "is assigned to"),
        ("symptom", "has symptom"),
        ("before", "started before"),
        ("similar", "is similar to"),
        ("duplicate", "may be the same event as"),
        ("mentions", "mentions"),
        ("confirmed", "was confirmed caused by"),
        ("covers", "is covered by article"),
        ("cites", "cites article"),
        ("targets", "is targeted by automation"),
        ("remediates", "is remediated by automation"),
        ("references", "references automation"),
        ("resolved_by", "was resolved by automation"),
    ]
    #: Relations a person makes. A rebuild never touches these.
    PERSON_MADE = {"confirmed"}
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(Application, on_delete=models.CASCADE)
    source = models.ForeignKey(OpsNode, on_delete=models.CASCADE, related_name="outgoing")
    target = models.ForeignKey(OpsNode, on_delete=models.CASCADE, related_name="incoming")
    relation = models.CharField(max_length=12, choices=RELATIONS)
    #: Strength where it has one: a similarity, hours before, a precedent score.
    weight = models.FloatField(default=0)
    #: Why, in words and numbers: reasons, shared symptoms, hours before, a quote.
    detail = models.JSONField(default=dict, blank=True)
    #: The verdict that asserted a `confirmed` edge; retracting it removes the edge.
    verdict = models.ForeignKey(
        TriageVerdict, null=True, blank=True, on_delete=models.CASCADE, related_name="edges"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["source", "target", "relation"], name="unique_ops_edge")
        ]
        indexes = [models.Index(fields=["application", "relation"])]


class AutomationConnection(models.Model):
    """Owner-configured execution boundary, separate from imported job descriptions."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(Application, on_delete=models.CASCADE)
    name = models.CharField(max_length=100)
    provider = models.CharField(max_length=20, choices=[("local", "Local Python runbooks"),
                                                       ("awx", "Ansible AWX")])
    base_url = models.URLField(max_length=500, blank=True)
    allowed_jobs = models.JSONField(default=list)
    enabled = models.BooleanField(default=True)
    require_approval = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class AutomationRun(models.Model):
    """Simulated requests and real, connection-bound automation executions."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(Application, on_delete=models.CASCADE)
    automation = models.ForeignKey(
        KnowledgeEntry, on_delete=models.PROTECT, related_name="automation_runs"
    )
    incident = models.ForeignKey(
        KnowledgeEntry,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="incident_automation_runs",
    )
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    mode = models.CharField(
        max_length=12, default="simulated",
        choices=[("simulated", "Simulated"), ("real", "Real execution")]
    )
    connection = models.ForeignKey(AutomationConnection, null=True, blank=True,
                                   on_delete=models.PROTECT)
    status = models.CharField(max_length=20, default="simulated", choices=[
        ("simulated", "Simulated"), ("pending_approval", "Awaiting approval"),
        ("queued", "Queued"), ("launching", "Launching"), ("running", "Running"),
        ("succeeded", "Succeeded"), ("failed", "Failed"), ("blocked", "Blocked"),
        ("unknown", "Needs reconciliation"),
    ])
    idempotency_key = models.UUIDField(null=True, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.PROTECT, related_name="automation_approvals")
    external_job_id = models.CharField(max_length=80, blank=True)
    result = models.JSONField(default=dict, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    platform = models.CharField(max_length=80, blank=True)
    #: What a real launch would have sent: method, address, inputs.
    request = models.JSONField(default=dict, blank=True)
    outcome = models.CharField(max_length=600, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["application", "incident", "created_at"])]
        constraints = [models.UniqueConstraint(fields=["application", "idempotency_key"],
            condition=models.Q(idempotency_key__isnull=False), name="unique_automation_request"),
            models.UniqueConstraint(fields=["application"], condition=models.Q(
                mode="real", status__in=["queued", "launching", "running"]),
                name="one_active_real_automation")]


class ServiceOpsTestRun(models.Model):
    """A fixed, isolated ServiceOps suite; never an arbitrary shell command."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(Application, on_delete=models.CASCADE)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    suite = models.CharField(max_length=20, default="e2e")
    status = models.CharField(max_length=12, default="queued", choices=[
        ("queued", "Queued"), ("running", "Running"), ("passed", "Passed"),
        ("failed", "Failed"), ("blocked", "Blocked"),
    ])
    results = models.JSONField(default=list, blank=True)
    summary = models.CharField(max_length=600, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(
            fields=["application"], condition=models.Q(status__in=["queued", "running"]),
            name="one_active_serviceops_test",
        ), models.UniqueConstraint(fields=["status"], condition=models.Q(status="running"),
                                   name="one_running_serviceops_test")]
