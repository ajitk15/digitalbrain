import uuid

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.text import slugify


class User(AbstractUser):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    is_platform_admin = models.BooleanField(default=False)
    must_change_password = models.BooleanField(default=False)


def unique_slug(model, name, field="slug"):
    """A readable, unique handle derived from a name.

    Two applications may legitimately share a name in different organizations,
    but one address space cannot, so a collision gets a short suffix rather than
    an error - naming is not the place to make someone retry.
    """
    base = slugify(name)[:120] or "application"
    candidate, index = base, 2
    while model.objects.filter(**{field: candidate}).exists():
        suffix = f"-{index}"
        candidate = f"{base[: 120 - len(suffix)]}{suffix}"
        index += 1
    return candidate


class NamedResource(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True
        ordering = ["name"]

    def __str__(self):
        return self.name


class Organization(NamedResource):
    active = models.BooleanField(default=True)


class OrganizationMember(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="members")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    is_admin = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "user"], name="unique_org_member")
        ]


class Portfolio(NamedResource):
    organization = models.ForeignKey(
        Organization, on_delete=models.PROTECT, related_name="portfolios"
    )


class Product(NamedResource):
    portfolio = models.ForeignKey(Portfolio, on_delete=models.PROTECT, related_name="products")


class Application(NamedResource):
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="applications")
    active = models.BooleanField(default=True)
    #: A readable handle for the API and MCP addresses, so a caller  does not
    #: have to carry a UUID around. Unique across the platform because those
    #: addresses have no organization in the path; guessing one gains nothing,
    #: since every endpoint is bearer-only and a token is bound to one
    #: application.
    slug = models.SlugField(max_length=140, unique=True, blank=True)
    #: When this application first had everything it needs to analyse a ticket.
    #:
    #: Derived state, recorded rather than recomputed. The setup strip appears on
    #: every application screen while onboarding is unfinished, and answering
    #: "is it unfinished?" costs half a dozen queries - which is fine while
    #: somebody is setting up and pure waste for every page view afterwards.
    #: Once earned it is never cleared: unpublishing a graph is a problem for the
    #: run that needs it, and the run says so. It is not a reason to start
    #: greeting somebody who onboarded months ago.
    setup_completed_at = models.DateTimeField(null=True, blank=True)
    identity_icon = models.CharField(max_length=24, blank=True)
    logo_digest = models.CharField(max_length=64, blank=True)

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = unique_slug(Application, self.name)
        super().save(*args, **kwargs)

    @property
    def organization_id(self):
        return self.product.portfolio.organization_id


class ApplicationLogo(models.Model):
    """The optional, sanitized PNG lives separately from application list rows."""

    application = models.OneToOneField(
        Application, on_delete=models.CASCADE, related_name="custom_logo", primary_key=True
    )
    png = models.BinaryField()


class ApplicationGrant(models.Model):
    class Role(models.TextChoices):
        VIEWER = "viewer", "Viewer"
        CONTRIBUTOR = "contributor", "Contributor"
        OWNER = "owner", "Application owner"

    application = models.ForeignKey(Application, on_delete=models.CASCADE, related_name="grants")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    role = models.CharField(max_length=16, choices=Role.choices)
    can_approve = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["application", "user"], name="unique_app_grant"),
            models.CheckConstraint(
                condition=models.Q(role__in=["viewer", "contributor", "owner"]),
                name="valid_app_role",
            ),
        ]


class Branding(models.Model):
    """Small canonical PNG kept in the database for atomic, multi-instance updates."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    png = models.BinaryField()
    digest = models.CharField(max_length=64)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(id=1), name="branding_singleton")]


class AuditEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    organization = models.ForeignKey(Organization, null=True, on_delete=models.PROTECT)
    action = models.CharField(max_length=60)
    details = models.JSONField(default=dict)
    request_id = models.CharField(max_length=36, blank=True)
    resource_id = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class FeatureSwitch(models.Model):
    key = models.CharField(max_length=40, primary_key=True)
    enabled = models.BooleanField(default=True)


class ApplicationFeature(models.Model):
    application = models.ForeignKey(Application, on_delete=models.CASCADE)
    key = models.CharField(max_length=40)
    enabled = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["application", "key"], name="unique_application_feature"
            )
        ]


class AIUsage(models.Model):
    purpose = models.CharField(max_length=24, default="chat")
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(Application, on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    provider = models.CharField(max_length=80)
    model = models.CharField(max_length=160)
    request_id = models.CharField(max_length=160)
    input_tokens = models.PositiveBigIntegerField()
    output_tokens = models.PositiveBigIntegerField()
    amount = models.DecimalField(max_digits=20, decimal_places=8)
    currency = models.CharField(max_length=3, default="USD")
    estimated = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["application", "created_at"])]
        constraints = [
            models.UniqueConstraint(
                fields=["application", "provider", "request_id"], name="unique_usage_receipt"
            ),
            models.CheckConstraint(condition=models.Q(amount__gte=0), name="nonnegative_ai_cost"),
        ]


class KnowledgeSource(models.Model):
    """A place documents came from, remembered so it can be checked again.

    A link import used to resolve a directory into files and keep only each
    file's own download address. Nothing recorded that twenty-seven documents
    shared an origin, so there was nothing to re-check, nothing to group them
    under, and no way to ask whether any of them had moved on.

    Modelled on `CodeRepository`, which had this shape for code all along: a
    registered thing with a status and a refresh somebody presses.

    **Drift is detected automatically; nothing is synchronised automatically.**
    The scheduled check reads one cheap signal per source and writes down that
    the origin has moved. Acting on that - downloading anything, changing any
    document - happens only when a person asks for it. The platform says what
    changed and waits, in the same way Code Factory describes work and stops.
    """

    class Status(models.TextChoices):
        READY = "ready", "In sync"
        STALE = "stale", "Out of sync"
        UNKNOWN = "unknown", "Not checked yet"
        UNREACHABLE = "unreachable", "Could not be checked"
        #: A folder someone uploaded from their own machine. There is no origin
        #: this server can reach, so it is never checked; uploading the folder
        #: again is how it is brought up to date.
        UPLOADED = "uploaded", "Uploaded"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(
        Application, on_delete=models.CASCADE, related_name="knowledge_sources"
    )
    added_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    #: Matches Document.origin, so a source and the documents under it agree.
    provider = models.CharField(max_length=10, default="link")
    #: The address a person pasted, normalised. Unique per application, so
    #: importing the same directory twice adds to one source instead of making
    #: a second one that drifts separately.
    url = models.CharField(max_length=2000)
    name = models.CharField(max_length=240)
    status = models.CharField(max_length=12, default=Status.UNKNOWN, choices=Status.choices)
    #: The cheap signal the last check read - a commit id for GitHub, a change
    #: token for a document library. Comparing it costs one request; comparing
    #: every file would cost one per file, which is the whole reason a scheduled
    #: check can afford to run at all.
    fingerprint = models.CharField(max_length=120, blank=True)
    #: What the last check found, in words meant for the person deciding
    #: whether to press Resync.
    drift_summary = models.CharField(max_length=500, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "created_at"]
        constraints = [
            models.UniqueConstraint(fields=["application", "url"], name="unique_knowledge_source")
        ]
        indexes = [models.Index(fields=["application", "status"])]

    def __str__(self):
        return self.name


class Document(models.Model):
    #: A document on its way in: queued to download, downloading, queued to
    #: convert, or converting. Every one of these is transient - the intake lane
    #: always moves a row out of them, to `ready` or to `failed`.
    #:
    #: Named once because three places disagreed about it. The graph worker
    #: waited only for `queued` and `converting`, so during a link import - where
    #: documents sit at `pending` while they queue for download one at a time -
    #: it saw no work in progress between each conversion and rebuilt the graph.
    #: A twenty-seven file import produced twenty-four saved versions.
    IN_FLIGHT = ("pending", "fetching", "queued", "converting")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(Application, on_delete=models.PROTECT, related_name="documents")
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    name = models.CharField(max_length=200)
    size = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64)
    status = models.CharField(
        max_length=20,
        default="quarantined",
        choices=[
            ("pending", "Waiting to download"),
            ("fetching", "Downloading"),
            ("quarantined", "Uploaded"),
            ("queued", "Queued for conversion"),
            ("converting", "Converting to Markdown"),
            ("deleted", "Deleted"),
            ("ready", "Ready to search"),
            ("failed", "Conversion failed"),
            ("rejected", "Rejected by scanner"),
        ],
    )
    # Where this document came from. A link-sourced document has no bytes until the
    # worker downloads them, which is what the pending and fetching states cover.
    origin = models.CharField(
        max_length=10,
        default="upload",
        choices=[
            ("upload", "Upload"),
            ("link", "Link"),
            ("github", "GitHub"),
            ("sharepoint", "SharePoint"),
            ("folder", "Folder"),
        ],
    )
    source_url = models.CharField(max_length=2000, blank=True)
    #: The place this document was imported from, when it came from one. Null
    #: for an upload, and for everything imported before sources were recorded.
    source = models.ForeignKey(
        "KnowledgeSource",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="documents",
    )
    #: Present at the origin as of the last resync. A file that disappears
    #: upstream is marked rather than removed: renaming a file upstream would
    #: otherwise delete a document that a published graph version cites, and
    #: nothing here removes a person's data without being asked.
    orphaned = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    conversion_started_at = models.DateTimeField(null=True, blank=True)
    conversion_actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="document_conversions",
    )
    conversion_error = models.CharField(max_length=500, blank=True)
    converter = models.CharField(max_length=80, blank=True)
    #: What conversion saw, read before the original can be removed: for a PDF
    #: its page count and the pages that held no text. The source check reads it.
    conversion_report = models.JSONField(default=dict, blank=True)
    #: When the stored original was deleted after converting, because the
    #: deployment does not keep originals. Name, size and SHA-256 remain.
    original_removed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["application", "created_at"])]


class KnowledgeEntryQuerySet(models.QuerySet):
    def answerable(self):
        """Active sources AI may read and cite: not archived, not restricted.

        Every path that hands a source to a model, or verifies a citation,
        starts here. See `sensitivity.py`.
        """
        return self.filter(active=True).exclude(sensitivity="restricted")


class KnowledgeEntry(models.Model):
    objects = KnowledgeEntryQuerySet.as_manager()

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(Application, on_delete=models.PROTECT)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    document = models.OneToOneField(Document, null=True, blank=True, on_delete=models.PROTECT)
    title = models.CharField(max_length=200)
    content = models.TextField(max_length=1000000)
    source = models.CharField(max_length=300, blank=True)
    digest = models.CharField(max_length=64)
    active = models.BooleanField(default=True)
    #: "restricted" keeps the source out of everything AI reads and cites.
    sensitivity = models.CharField(
        max_length=12,
        choices=[("standard", "Standard"), ("restricted", "Restricted - never used by AI")],
        default="standard",
    )
    #: What `sensitivity.scan` found when the source was added: {kind: count}.
    #: A suggestion for a person, never a label by itself.
    personal_data = models.JSONField(default=dict, blank=True)
    #: What `masking` replaced before the text was stored: {kind: count}. The
    #: values themselves are never kept; `personal_data` is what is still there.
    masked = models.JSONField(default=dict, blank=True)
    #: What `source_quality.run` found when the source was added: the rules'
    #: version and each check. Empty until checked; stale rules are re-run on read.
    quality = models.JSONField(default=dict, blank=True)
    #: The kind of document a person said this is, a `document_types` key. Blank
    #: means the type the rules detect, which `quality` records either way.
    document_type = models.CharField(max_length=40, blank=True)
    #: Who the source system lets open this record, as platform user ids. None
    #: means its system is not checked and every member may see it in answers;
    #: a list - empty until the first check - limits answers to those users.
    #: See `permissions.py`.
    access_principals = models.JSONField(null=True, blank=True)
    access_checked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["application", "active"])]


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


class CodeRepository(models.Model):
    """A repository registered inside one application's security boundary."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(
        Application, on_delete=models.PROTECT, related_name="code_repositories"
    )
    added_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    provider = models.CharField(max_length=20, default="github")
    external_id = models.CharField(max_length=240)
    name = models.CharField(max_length=240)
    source_url = models.URLField(max_length=1000)
    default_ref = models.CharField(max_length=200, default="main")
    job_id = models.UUIDField(default=uuid.uuid4)
    status = models.CharField(
        max_length=20,
        default="queued",
        choices=[
            ("documentation", "Source import only"),
            ("queued", "Queued"),
            ("indexing", "Indexing"),
            ("ready", "Ready"),
            ("partial", "Partial"),
            ("failed", "Failed"),
        ],
    )
    error = models.CharField(max_length=500, blank=True)
    #: What the repository's default branch was pointing at when this was last
    #: checked, and when. A snapshot is a commit; the branch moves on without
    #: it, and a code graph answering from a commit somebody force-pushed past
    #: three weeks ago should say so rather than look current. Nothing is
    #: fetched automatically from it - drift is detected, never acted on, which
    #: is the rule knowledge sources already follow.
    head_sha = models.CharField(max_length=64, blank=True)
    head_checked_at = models.DateTimeField(null=True, blank=True)
    #: Retired rather than deleted. `CodeSnapshot.repository` is PROTECT and
    #: `FactoryRun.code_snapshot` is SET_NULL, so deleting a repository would
    #: either be refused or silently blank the code pin on every past run that
    #: reasoned about it - rewriting the record of what those runs were given.
    #: Retiring takes it out of every live query and leaves the history intact,
    #: the same trade `connectors.prune` makes for knowledge it supersedes.
    retired_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def drifted(self):
        """Whether the default branch has moved past the indexed snapshot.

        Unknown until both are known: a repository nobody has checked and one
        that is exactly current must not read the same, so this is None rather
        than False when there is nothing to compare.
        """
        snapshot = self.snapshots.first()
        if not self.head_sha or snapshot is None or not snapshot.commit_sha:
            return None
        return self.head_sha != snapshot.commit_sha

    class Meta:
        ordering = ["name", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["application", "provider", "external_id"],
                name="unique_application_code_repository",
            )
        ]
        indexes = [models.Index(fields=["application", "status"])]


class CodeSnapshot(models.Model):
    """Immutable analysis of one repository at one resolved commit."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    repository = models.ForeignKey(
        CodeRepository, on_delete=models.PROTECT, related_name="snapshots"
    )
    number = models.PositiveIntegerField()
    ref = models.CharField(max_length=200)
    commit_sha = models.CharField(max_length=64)
    manifest_digest = models.CharField(max_length=64)
    analyzer_version = models.CharField(max_length=40, default="structural-v1")
    complete = models.BooleanField(default=True)
    warnings = models.JSONField(default=list)
    #: Number of circular *groups*, not files in them: five files in one loop is
    #: one problem. Counted across every file, not the drawn ones.
    cycle_count = models.PositiveIntegerField(default=0)
    orphan_count = models.PositiveIntegerField(default=0)
    #: The test framework and CI the repository declares, read from the clone
    #: (`repo_testing.detect`). Configuration is not source and is not indexed,
    #: so an offline test author has no other way to know. Empty on snapshots
    #: taken before this was recorded, which read as "not known".
    test_setup = models.JSONField(default=dict, blank=True)
    #: Which languages the repository is written in, counted over every file
    #: in the clone rather than the indexed subset (`code_graph_analysis.census`).
    #: Names come from a closed set, so they are safe to put in a prompt. Empty
    #: on snapshots taken before this was recorded, which read as "not known".
    languages = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-number"]
        constraints = [
            models.UniqueConstraint(
                fields=["repository", "number"], name="unique_code_snapshot_number"
            ),
            models.UniqueConstraint(
                fields=["repository", "commit_sha", "manifest_digest"],
                name="unique_code_snapshot_manifest",
            ),
        ]
        indexes = [models.Index(fields=["repository", "-created_at"])]


class CodeFile(models.Model):
    """A bounded source file and parser facts retained with its snapshot."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    snapshot = models.ForeignKey(CodeSnapshot, on_delete=models.CASCADE, related_name="files")
    path = models.CharField(max_length=500)
    language = models.CharField(max_length=30)
    digest = models.CharField(max_length=64)
    content = models.TextField(max_length=400000)
    parse_ok = models.BooleanField(default=True)
    symbols = models.JSONField(default=list)
    imports = models.JSONField(default=list)
    #: HTTP routes this file declares, and outbound calls it makes. Both sides
    #: of an API seam, kept so an inferred edge can show its evidence.
    routes = models.JSONField(default=list)
    calls = models.JSONField(default=list)
    lines = models.PositiveIntegerField(default=0)
    #: Decided over the whole snapshot at index time, never over the subset a
    #: page draws. Blank on snapshots taken before roles were stored.
    role = models.CharField(max_length=12, blank=True)
    #: Which circular group, when this file is in one. Groups are numbered per
    #: snapshot; a file importing itself is a group of one.
    cycle_group = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["path", "id"]
        constraints = [
            models.UniqueConstraint(fields=["snapshot", "path"], name="unique_code_snapshot_path")
        ]
        indexes = [models.Index(fields=["snapshot", "language"])]


class CodeRelationship(models.Model):
    """A directed, explainable relationship: source depends on target."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    snapshot = models.ForeignKey(
        CodeSnapshot, on_delete=models.CASCADE, related_name="relationships"
    )
    source = models.ForeignKey(CodeFile, on_delete=models.CASCADE, related_name="dependencies")
    target = models.ForeignKey(CodeFile, on_delete=models.CASCADE, related_name="dependents")
    kind = models.CharField(max_length=20, default="import")
    confidence = models.CharField(
        max_length=12, choices=[("static", "Static"), ("inferred", "Inferred")]
    )
    detail = models.CharField(max_length=500, blank=True)
    evidence = models.JSONField(default=dict)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["snapshot", "source", "target", "kind"],
                name="unique_code_relationship",
            )
        ]
        indexes = [
            models.Index(fields=["snapshot", "source"]),
            models.Index(fields=["snapshot", "target"]),
        ]


AI_STEP_STATUSES = [
    ("", "Not asked"),
    ("queued", "Queued"),
    ("running", "Running"),
    ("ready", "Ready"),
    ("failed", "Failed"),
]


class CodeReview(models.Model):
    """A change compared with the snapshot it starts from. See `code_review.py`.

    The base is always a stored `CodeSnapshot`; the head is what the change
    reads as now - a folder on disk, a branch, a pull request - and the files it
    touches are kept in `CodeReviewFile` so a citation to the change stays
    checkable after the branch moves on. Nothing here is posted anywhere.
    """

    KINDS = [
        ("local", "Working changes in the folder"),
        ("branch", "GitHub branch"),
        ("pull_request", "Pull request"),
    ]
    STATUSES = [
        ("queued", "Queued"),
        ("reading", "Reading the change"),
        ("ready", "Ready"),
        ("failed", "Failed"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(Application, on_delete=models.CASCADE)
    repository = models.ForeignKey(CodeRepository, on_delete=models.CASCADE)
    snapshot = models.ForeignKey(CodeSnapshot, on_delete=models.CASCADE, related_name="reviews")
    kind = models.CharField(max_length=14, choices=KINDS)
    #: The branch reviewed, or the pull request's head branch once resolved.
    ref = models.CharField(max_length=200, blank=True)
    number = models.PositiveIntegerField(null=True, blank=True)
    #: owner/name the head was read from: a fork, for a pull request from one.
    head_repository = models.CharField(max_length=240, blank=True)
    head_commit = models.CharField(max_length=64, blank=True)
    merge_base = models.CharField(max_length=64, blank=True)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    status = models.CharField(max_length=10, choices=STATUSES, default="queued")
    job_id = models.UUIDField(default=uuid.uuid4)
    error = models.CharField(max_length=500, blank=True)
    warnings = models.JSONField(default=list, blank=True)
    #: What the rules found, with no model: see `code_review.summarise`.
    summary = models.JSONField(default=dict, blank=True)
    ai_status = models.CharField(max_length=10, choices=AI_STEP_STATUSES, blank=True)
    ai_requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    ai_job_id = models.UUIDField(default=uuid.uuid4)
    ai_error = models.CharField(max_length=500, blank=True)
    #: What the model said, after every quote was checked. Also kept in
    #: `DocumentAIResult` by what it read, so asking again costs nothing.
    ai_result = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["application", "-created_at"]),
            models.Index(fields=["status", "updated_at"]),
        ]


class CodeReviewFile(models.Model):
    """One file a reviewed change touches: both digests and the head's text."""

    CHANGES = [("added", "Added"), ("modified", "Modified"), ("removed", "Removed")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    review = models.ForeignKey(CodeReview, on_delete=models.CASCADE, related_name="files")
    path = models.CharField(max_length=500)
    change = models.CharField(max_length=10, choices=CHANGES)
    language = models.CharField(max_length=30, blank=True)
    base_digest = models.CharField(max_length=64, blank=True)
    head_digest = models.CharField(max_length=64, blank=True)
    head_content = models.TextField(blank=True)
    diff = models.TextField(blank=True)
    added = models.PositiveIntegerField(default=0)
    removed = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["path", "id"]
        constraints = [
            models.UniqueConstraint(fields=["review", "path"], name="unique_review_file_path")
        ]


class CodeDocumentation(models.Model):
    """Documentation written from one snapshot. See `code_docs.py`.

    Generated by rules, with no model; an AI part is added only on request and
    every paragraph of it quotes the code. It becomes a knowledge source only
    when someone adds it (`knowledge_entry`).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(Application, on_delete=models.CASCADE)
    repository = models.ForeignKey(CodeRepository, on_delete=models.CASCADE)
    snapshot = models.ForeignKey(
        CodeSnapshot, on_delete=models.CASCADE, related_name="documentation"
    )
    generated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    #: Which rules wrote it (`code_docs.DOCS_VERSION`) and the knowledge graph
    #: version its traceability read, so a page knows when it is out of date.
    version = models.PositiveIntegerField(default=1)
    graph_version = models.PositiveIntegerField(null=True, blank=True)
    sections = models.JSONField(default=dict, blank=True)
    ai_status = models.CharField(max_length=10, choices=AI_STEP_STATUSES, blank=True)
    ai_requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    ai_job_id = models.UUIDField(default=uuid.uuid4)
    ai_error = models.CharField(max_length=500, blank=True)
    #: What the model said, after every quote was checked. Also kept in
    #: `DocumentAIResult` by what it read, so asking again costs nothing.
    ai_result = models.JSONField(default=dict, blank=True)
    knowledge_entry = models.ForeignKey(
        "KnowledgeEntry", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["application", "-created_at"])]


CHAT_MODES =[("search", "Sources"), ("ai", "AI"), ("graph", "Graph answer")]

MESSAGE_ROLES = [("user", "You"), ("assistant", "Digital Brain")]

MESSAGE_STATUSES = [
    ("streaming", "Streaming"),
    ("complete", "Complete"),
    ("stopped", "Stopped"),
    ("failed", "Failed"),
]


class ChatConversation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(Application, on_delete=models.PROTECT)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    title = models.CharField(max_length=120)
    # Answer mode belongs to the conversation, not the message: a conversation is
    # an AI conversation or a source-search conversation, and sending a message
    # must never be able to flip it into a paid mode by accident.
    mode = models.CharField(max_length=10, default="ai", choices=CHAT_MODES)
    # Which saved graph version graph answers read from. Null means the live graph.
    graph_version = models.PositiveIntegerField(null=True, blank=True)
    title_locked = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "-id"]
        indexes = [models.Index(fields=["application", "user", "-updated_at"])]


class ChatRetention(models.Model):
    """How long an application keeps chat conversations.

    Per application rather than platform-wide: retention is a data-governance
    choice that differs between applications. `days = 0` means keep indefinitely,
    which is why this is not a nullable integer - "0" reads unambiguously in a
    form, whereas a blank field reads as "unset" and invites a wrong default.
    """

    DEFAULT_DAYS = 7

    application = models.OneToOneField(Application, on_delete=models.CASCADE)
    days = models.PositiveIntegerField(default=DEFAULT_DAYS)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )


class ChatMessage(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(Application, on_delete=models.PROTECT)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    conversation = models.ForeignKey(
        ChatConversation, on_delete=models.CASCADE, related_name="messages"
    )
    role = models.CharField(max_length=10, choices=MESSAGE_ROLES)
    status = models.CharField(max_length=12, default="complete", choices=MESSAGE_STATUSES)
    body = models.TextField(max_length=200000, blank=True)
    citations = models.JSONField(default=list)
    # What actually produced this answer, so the transcript stays truthful even
    # after the conversation's own mode is changed.
    mode = models.CharField(max_length=10, default="ai", choices=CHAT_MODES)
    provider = models.CharField(max_length=16, blank=True)
    model = models.CharField(max_length=160, blank=True)
    error = models.CharField(max_length=300, blank=True)
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="regenerations"
    )
    # Both halves of one exchange are written in the same transaction, so
    # created_at cannot order them; an explicit sequence can.
    sequence = models.PositiveIntegerField()
    #: The composer's one-time key, on the question it sent: which send asked
    #: it. Whether a send is a repeat is decided by `ChatSubmission`, whose
    #: unique constraint makes the claim atomic; this is the record.
    submission = models.CharField(max_length=36, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["sequence", "created_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["conversation", "sequence"], name="unique_conversation_sequence"
            )
        ]
        indexes = [
            models.Index(fields=["conversation", "sequence"]),
            models.Index(fields=["application", "user", "submission"]),
        ]


class ChatSubmission(models.Model):
    """One send from the chat composer, claimed before anything is done for it.

    The claim is the first write, in its own transaction, and the unique
    constraint is what makes it atomic: two overlapping requests with the same
    key cannot both succeed, so only one ever reaches the provider. Checking for
    an existing message first was not enough - two requests could both look,
    find nothing, and both ask.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(Application, on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    key = models.CharField(max_length=36)
    #: Set once the send has a conversation, so a repeat can be shown it.
    conversation = models.ForeignKey(
        ChatConversation, null=True, blank=True, on_delete=models.CASCADE
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["application", "user", "key"], name="unique_chat_submission"
            )
        ]


class ApiToken(models.Model):
    """A machine credential that acts as one user within one application.

    Deliberately not a new kind of principal. The token carries a user and an
    application, and every request it makes runs the same access checks a browser
    session would - so revoking that user's grant, disabling their account or
    turning off a feature switch takes the token with it, and there is no second
    permission model to drift out of step with the first.

    Only a hash of the secret is stored. The value is shown once, at creation.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(Application, on_delete=models.CASCADE, related_name="tokens")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    name = models.CharField(max_length=120)
    #: The public half, used to find the row before verifying the secret.
    prefix = models.CharField(max_length=16, unique=True)
    digest = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["application", "-created_at"])]

    @property
    def active(self):
        from django.utils import timezone as tz

        if self.revoked_at:
            return False
        return not (self.expires_at and self.expires_at <= tz.now())

    @property
    def state(self):
        from django.utils import timezone as tz

        if self.revoked_at:
            return "Revoked"
        if self.expires_at and self.expires_at <= tz.now():
            return "Expired"
        return "Active"


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


CONNECTOR_KINDS = [
    ("github", "GitHub"),
    ("jira", "Jira"),
    ("servicenow", "ServiceNow"),
    ("awx", "Ansible AWX"),
    ("rundeck", "Rundeck"),
    ("azure_automation", "Azure Automation"),
    ("automation_list", "Automation list"),
]


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
    #: `pull_request_url`: set once, by `code_factory_build.apply_local`.
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


class Connector(models.Model):
    """One external system an application imports knowledge from.

    A foreign key rather than a one-to-one: an application can legitimately track
    a GitHub repository, a Jira project and a ServiceNow table at once, and two
    Jira projects besides. `config` holds the settings a kind needs and nothing
    secret - credentials stay file-mounted, so the non-secret half of an identity
    (an account email, a client id) lives here while its token never does.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(Application, on_delete=models.PROTECT)
    kind = models.CharField(max_length=20, choices=CONNECTOR_KINDS, default="github")
    name = models.CharField(max_length=120, default="")
    config = models.JSONField(default=dict, blank=True)
    enabled = models.BooleanField(default=True)
    #: Minutes between unattended imports, or 0 for manual only. A ticket board
    #: whose status only changes in the source system is stale knowledge the
    #: moment somebody moves a card, and pressing Import is not a schedule.
    sync_interval_minutes = models.PositiveIntegerField(default=0)
    #: Opt-in, and deliberately not the default. When the filter returns the
    #: whole set, a record that stops appearing has been deleted and its
    #: knowledge should stop being citable. When the filter is narrow - "updated
    #: in the last 30 days" - absence means nothing of the sort, which is why the
    #: owner has to state that their filter is complete.
    prune_missing = models.BooleanField(default=False)
    #: Answers cite a record only for members the source system lets open it.
    #: Opt-in; Jira only for now. See `permissions.py`.
    enforce_permissions = models.BooleanField(default=False)
    # blank=True as well as null=True: a connector that has never run has no
    # date, and full_clean refuses a null in a field that is not also blank -
    # which made every new connector unsaveable through the form.
    last_synced_at = models.DateTimeField(null=True, blank=True)
    #: When an import was last *attempted*, successfully or not. The schedule is
    #: measured from this rather than from last_synced_at, so a connector that
    #: keeps failing waits its interval instead of being retried on every tick.
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    last_count = models.PositiveIntegerField(default=0)
    # What happened last time, so a failure leaves a trace on the row rather than
    # only in a log nobody reads.
    last_status = models.CharField(max_length=12, blank=True)
    last_error = models.TextField(blank=True)
    last_duration_ms = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["kind", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["application", "kind", "name"], name="unique_connector_name"
            )
        ]

    def __str__(self):
        return self.name or self.get_kind_display()


#: In the order AI settings lists them: the features people use every day first,
#: then the graph's own tasks, then the small housekeeping call.
AI_PURPOSES = [
    ("chat", "Chat conversation"),
    ("plan_drafting", "Code Factory"),
    ("serviceops_triage", "ServiceOps"),
    ("graph_generation", "Graph generation"),
    ("graph_retrieval", "Graph retrieval"),
    ("conversation_title", "Conversation titles"),
    ("embeddings", "Search embeddings"),
    ("reranking", "Answer reranking"),
    ("document_typing", "Document typing"),
    ("document_review", "Document review"),
    ("code_review", "Code review"),
    ("code_docs", "Code documentation"),
]
AI_PROVIDERS = [("openai", "OpenAI Agents SDK"), ("claude", "Claude Agent SDK")]


class ManagedCredential(models.Model):
    """Provenance for a credential an owner set from the browser.

    The credential itself is **not** here. It lives in a file with owner-only
    permissions, the same as one an operator mounts, and `secrets.py` is the only
    thing that writes it. This row records who set it and when, plus a digest
    salted with the file name so it can be shown for confirmation without being
    comparable across applications.

    The file is the truth. A row whose file has been removed under it means the
    credential is gone, not that it is still configured - which is why the screen
    reads presence from disk and uses this only for the "set by" line.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(Application, on_delete=models.CASCADE)
    #: The registry key - "jira", "openai" - never the file name, which is
    #: derived from this and the application id where the file is written.
    name = models.CharField(max_length=40)
    digest = models.CharField(max_length=64)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["application", "name"], name="unique_managed_credential"
            )
        ]

    def __str__(self):
        return f"{self.name} for {self.application_id}"


class AIConfiguration(models.Model):
    application = models.ForeignKey(Application, on_delete=models.PROTECT)
    purpose = models.CharField(max_length=24, choices=AI_PURPOSES, default="chat")
    provider = models.CharField(max_length=12, choices=AI_PROVIDERS, default="openai")
    configured_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )
    model = models.CharField(max_length=160)
    enabled = models.BooleanField(default=True)
    input_rate = models.DecimalField(max_digits=12, decimal_places=6)
    output_rate = models.DecimalField(max_digits=12, decimal_places=6)
    #: The reply cap, when an owner has set one. Only the purposes in
    #: `ai.output_limits` read it; empty means that task's default.
    output_limit = models.PositiveIntegerField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(input_rate__gte=0, output_rate__gte=0),
                name="nonnegative_ai_rates",
            ),
            models.UniqueConstraint(fields=["application", "purpose"], name="unique_ai_purpose"),
        ]


class KnowledgeGraph(models.Model):
    """The working graph: always current with sources, not necessarily published."""

    application = models.OneToOneField(Application, on_delete=models.CASCADE)
    version = models.UUIDField(default=uuid.uuid4)
    fingerprint = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=16, default="building")
    data = models.JSONField(default=dict)
    quality = models.JSONField(default=dict)
    # A requested run carries its own model choice. AI enrichment happens only when
    # someone asked for it, so a background rebuild can never incur provider charges.
    requested_provider = models.CharField(max_length=12, blank=True)
    requested_model = models.CharField(max_length=160, blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="graph_runs",
    )
    # Progress for a run in flight. `stage` is a short human phrase written at each
    # real transition, never a guess: a provider call is one opaque block, so the
    # page reports the step it is actually in and how long it has been there rather
    # than animating a percentage nobody can compute.
    started_at = models.DateTimeField(null=True, blank=True)
    stage = models.CharField(max_length=120, blank=True)
    # Why the last run failed, in the sanitized wording the user may see. Kept so
    # the page can say what to do instead of only that something went wrong.
    failure_reason = models.TextField(blank=True)
    #: Who asked for AI relationships to be kept current, and pays for it. When
    #: set, a change to the sources queues an incremental AI run as this person,
    #: re-checking their grant first - revoking it stops the refresh, with no
    #: code that knows about refreshes. Empty means off, the default.
    auto_refresh_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    #: Why automatic refresh last stopped by itself, for the owner to read.
    auto_refresh_note = models.CharField(max_length=300, blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class GraphRevision(models.Model):
    """A saved snapshot. Only a published one is used to answer questions."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(Application, on_delete=models.CASCADE)
    number = models.PositiveIntegerField()
    fingerprint = models.CharField(max_length=64)
    data = models.JSONField(default=dict)
    quality = models.JSONField(default=dict)
    provider = models.CharField(max_length=12, blank=True)
    model = models.CharField(max_length=160, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="published_graphs",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-number"]
        constraints = [
            models.UniqueConstraint(fields=["application", "number"], name="unique_graph_revision")
        ]
        indexes = [models.Index(fields=["application", "-published_at"])]

    @property
    def version(self):
        return self.pk

    @property
    def updated_at(self):
        return self.created_at


class BenchmarkQuestion(models.Model):
    """A question this application's graph should answer, and what it should cite.

    `expected` is text the cited source's title or URL must contain - a ticket
    key such as "KAN-4", a document name such as "deployment-runbook" - rather
    than a source id, because a source that is re-imported gets a new id and a
    question should keep meaning the same thing across versions.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(Application, on_delete=models.CASCADE)
    question = models.CharField(max_length=500)
    expected = models.CharField(max_length=200)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]


class BenchmarkRun(models.Model):
    """How one graph version did on the benchmark questions it was run against.

    Retrieval only: the questions go through the same ranking and citation
    verification an answer uses, and no model is called, so a run is free and
    can be repeated on every version. `results` keeps each question's outcome,
    so a score can be explained rather than taken on trust.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(Application, on_delete=models.CASCADE)
    revision = models.ForeignKey(GraphRevision, on_delete=models.CASCADE)
    questions = models.PositiveIntegerField()
    #: The expected source was among the citations an answer would receive.
    found = models.PositiveIntegerField()
    #: ...and among the first three, which an answer leans on most.
    top_three = models.PositiveIntegerField()
    #: Mean reciprocal rank: 1.0 when every expected source is cited first.
    reciprocal_rank = models.FloatField()
    results = models.JSONField(default=list)
    TRIGGERS = [
        ("saved", "When the version was saved"),
        ("button", "From Knowledge"),
        ("console", "From the console"),
    ]
    trigger = models.CharField(max_length=8, choices=TRIGGERS, default="saved")
    #: Who pressed Run; empty for the automatic and console runs.
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["application", "revision", "-created_at"])]


class Embedding(models.Model):
    """A text's meaning as a vector, for finding where to start looking.

    Content-addressed: `digest` is the SHA-256 of the exact text embedded, so a
    relationship or passage that has not changed is never paid for twice, and
    a graph version rebuilt from the same sources reuses every vector. Scoped
    to one application - texts are that application's content, and one
    application's spend must never serve another.

    A vector only ever chooses starting points. It is never evidence: what is
    cited is still a relationship or passage verified against its live source.
    `entry` and `offset` locate a passage so it can be re-read and re-checked;
    they are empty for graph relationships and questions.
    """

    KINDS = [("relation", "Graph relationship"), ("passage", "Passage"), ("question", "Question")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(Application, on_delete=models.CASCADE)
    model = models.CharField(max_length=160)
    dimensions = models.PositiveSmallIntegerField()
    digest = models.CharField(max_length=64)
    kind = models.CharField(max_length=8, choices=KINDS)
    #: Unit-length float32, little-endian: a dot product is the cosine.
    vector = models.BinaryField()
    entry = models.ForeignKey(
        KnowledgeEntry, null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    offset = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["application", "model", "dimensions", "digest"],
                name="unique_embedding_text",
            )
        ]
        indexes = [models.Index(fields=["application", "model", "dimensions", "kind"])]


class AIBudget(models.Model):
    """An application's monthly cap on recorded AI spend. No row means no cap.

    Checked by `budget.check` before every paid call; see `budget.py` for what
    it guarantees and what it does not.
    """

    application = models.OneToOneField(Application, on_delete=models.CASCADE)
    monthly_limit = models.DecimalField(max_digits=12, decimal_places=2)
    set_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(monthly_limit__gte=0), name="nonnegative_ai_budget"
            )
        ]


class PersonalDataPolicy(models.Model):
    """Which personal data an application masks before a source is stored.

    No row, or `enabled` off, means nothing is masked: the scan only suggests,
    as it always has. See `masking.py` for what masking does and does not do.
    """

    application = models.OneToOneField(Application, on_delete=models.CASCADE)
    enabled = models.BooleanField(default=False)
    #: `sensitivity.PATTERNS` kinds, plus "payment card number".
    kinds = models.JSONField(default=list, blank=True)
    set_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    updated_at = models.DateTimeField(auto_now=True)


class StreamLease(models.Model):
    """A chat answer being streamed, visible to every server process.

    Only written when `settings.SHARED_STREAMS` is on. The process streaming the
    answer renews `heartbeat_at`; any process may set `stop_requested`, which
    the streaming process polls. A lease whose heartbeat has gone quiet belongs
    to a process that died, and is treated as gone.
    """

    message = models.OneToOneField("ChatMessage", primary_key=True, on_delete=models.CASCADE)
    owner = models.CharField(max_length=64)
    heartbeat_at = models.DateTimeField()
    stop_requested = models.BooleanField(default=False)


class ExternalIdentity(models.Model):
    """Which account a platform user is in one external system.

    Found by email the first time a permission check needs it, or set by an
    operator (`manage.py link_identity`) where the system hides email
    addresses. One per user, system and site.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    system = models.CharField(max_length=20)
    site = models.CharField(max_length=200)
    #: The system's own id: a Jira Cloud accountId, a Data Center username. Empty
    #: when the lookup found nobody, so the miss is not repeated every sync.
    account = models.CharField(max_length=200, blank=True)
    method = models.CharField(max_length=8, choices=[("email", "By email"), ("manual", "Set")])
    resolved_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "system", "site"], name="unique_identity")
        ]


class SignInIdentity(models.Model):
    """The Microsoft Entra ID account that signs a platform user in.

    Kept apart from `ExternalIdentity` on purpose: that table says who someone
    is in Jira for filtering answers and is written by email lookups, while a
    row here lets its holder sign in. Keyed on the tenant and the account's
    object id (`oid`), which Entra never reuses or lets anyone rename; the
    sign-in name is kept only to show. Once linked, an account is matched by
    `oid` alone, so a name reassigned in the directory later cannot take it.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="sign_in_identities"
    )
    tenant = models.CharField(max_length=36)
    subject = models.CharField(max_length=36)
    name = models.CharField(max_length=254, blank=True)
    method = models.CharField(
        max_length=8,
        choices=[
            ("name", "Matched by sign-in name"),
            ("created", "Created at first sign-in"),
            ("manual", "Linked by an operator"),
        ],
    )
    created_at = models.DateTimeField(auto_now_add=True)
    last_signed_in_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["tenant", "subject"], name="unique_sign_in_subject"),
            models.UniqueConstraint(fields=["user", "tenant"], name="unique_sign_in_user"),
        ]


class DocumentAIResult(models.Model):
    """What a model said about a document's text, kept so it is paid for once.

    Content-addressed like `Embedding`: `digest` is the SHA-256 of the exact
    text the model read (`KnowledgeEntry.digest`), and `rubric` names the rules
    it was asked under - the registry version for typing, the review rubric and
    the type it reviewed against for a review - so a rule change asks again and
    nothing else does. Scoped to one application: one application's spend never
    serves another. A verdict the checks refused is kept too, with why: the
    call was paid for, and asking again would pay for the same refusal.

    Every quote in `result` was found in the text exactly when it was stored.
    See `document_ai.py`.
    """

    TASKS = [
        ("typing", "Document typing"),
        ("review", "Document review"),
        # Code Graph keeps its paid results here too, keyed the same way: the
        # digest of exactly what the model read, and the rubric it was asked
        # under. See `code_review.py` and `code_docs.py`.
        ("change", "Code review"),
        ("codedocs", "Code documentation"),
        # Knowledge graph extraction, one row per window a model read: the
        # digest is of the title and window text (`graph_ai.window_key`), the
        # rubric the ontology version. See `graph_ai.enrich_graph`.
        ("extract", "Graph extraction"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(Application, on_delete=models.CASCADE)
    task = models.CharField(max_length=8, choices=TASKS)
    digest = models.CharField(max_length=64)
    rubric = models.CharField(max_length=80)
    result = models.JSONField(default=dict)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["application", "task", "digest", "rubric"],
                name="unique_document_ai_result",
            )
        ]
