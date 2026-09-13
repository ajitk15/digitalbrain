import uuid

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    is_platform_admin = models.BooleanField(default=False)
    must_change_password = models.BooleanField(default=False)


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


class Document(models.Model):
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
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class Connector(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.OneToOneField(Application, on_delete=models.PROTECT)
    repository = models.CharField(max_length=200)
    enabled = models.BooleanField(default=True)
    last_synced_at = models.DateTimeField(null=True)
    last_count = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)


AI_PURPOSES = [
    ("chat", "Chat conversation"),
    ("graph_generation", "Graph generation"),
    ("graph_retrieval", "Graph retrieval"),
    ("conversation_title", "Conversation titles"),
    ("plan_drafting", "Code Factory drafting"),
]
AI_PROVIDERS = [("openai", "OpenAI Agents SDK"), ("claude", "Claude Agent SDK")]


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
