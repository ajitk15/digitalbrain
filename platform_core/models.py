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

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = unique_slug(Application, self.name)
        super().save(*args, **kwargs)

    @property
    def organization_id(self):
        return self.product.portfolio.organization_id


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
            models.UniqueConstraint(
                fields=["application", "url"], name="unique_knowledge_source"
            )
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

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["application", "created_at"])]


class KnowledgeEntry(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(Application, on_delete=models.PROTECT)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    document = models.OneToOneField(Document, null=True, blank=True, on_delete=models.PROTECT)
    title = models.CharField(max_length=200)
    content = models.TextField(max_length=1000000)
    source = models.CharField(max_length=300, blank=True)
    digest = models.CharField(max_length=64)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["application", "active"])]


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
    #: Retired rather than deleted. `CodeSnapshot.repository` is PROTECT and
    #: `FactoryRun.code_snapshot` is SET_NULL, so deleting a repository would
    #: either be refused or silently blank the code pin on every past run that
    #: reasoned about it - rewriting the record of what those runs were given.
    #: Retiring takes it out of every live query and leaves the history intact,
    #: the same trade `connectors.prune` makes for knowledge it supersedes.
    retired_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

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


CHAT_MODES = [("search", "Sources"), ("ai", "AI"), ("graph", "Graph answer")]

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
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["sequence", "created_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["conversation", "sequence"], name="unique_conversation_sequence"
            )
        ]
        indexes = [models.Index(fields=["conversation", "sequence"])]


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
]


#: What an analysis item is about. A ticket states some of what it wants; the
#: rest is found by comparing the ticket against what the graph says the system
#: actually does, and against the non-functional rubric.
ITEM_CATEGORIES = [
    ("stated", "Stated in the ticket"),
    ("functional", "Functional gap"),
    ("non_functional", "Non-functional gap"),
]

#: Non-functional headings, taken from docs/non-functional.md so the analysis
#: reasons in the same terms the project already documents itself in.
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
    ("implementation", "Implementation"),
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
    application = models.ForeignKey(Application, on_delete=models.PROTECT)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    connector = models.ForeignKey(
        "Connector", null=True, blank=True, on_delete=models.SET_NULL
    )
    # The ticket, copied rather than referenced: a run has to stay readable after
    # the source entry is superseded by a later import.
    ticket_external_id = models.CharField(max_length=120, blank=True)
    ticket_title = models.CharField(max_length=300, blank=True)
    ticket_url = models.URLField(max_length=1000, blank=True)
    ticket_digest = models.CharField(max_length=64, blank=True)
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
            ("delivering", "Delivering"),
            ("delivered", "Pull request opened"),
            ("complete", "Complete"),
            ("failed", "Failed"),
        ],
    )
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["application", "-created_at"])]

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
        return self.status in {"pending", "running", "delivering"}


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
        constraints = [
            models.UniqueConstraint(fields=["run", "name"], name="unique_run_phase")
        ]


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


AI_PURPOSES = [
    ("chat", "Chat conversation"),
    ("graph_generation", "Graph generation"),
    ("graph_retrieval", "Graph retrieval"),
    ("conversation_title", "Conversation titles"),
    ("plan_drafting", "Code Factory drafting"),
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
