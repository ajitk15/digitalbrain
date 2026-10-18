"""What the platform knows: sources, documents, entries, passages, graphs."""

import uuid

from django.conf import settings
from django.db import models

from .accounts import Application


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


#: The fields a graph is built from. Writing any of them asks the graph lane to
#: look at the application again (`triggers.py`); writing quality, a document
#: type or a restriction note does not.
GRAPH_FIELDS = frozenset(
    {"active", "sensitivity", "digest", "content", "application", "application_id"}
)
#: The fields a source's passages are cut from (`chunks.py`).
CHUNK_FIELDS = frozenset({"active", "content", "title", "digest"})


class KnowledgeEntryQuerySet(models.QuerySet):
    """`update` and `delete` say which applications they touched.

    Neither sends a signal, and both are how most sources change - supersede,
    restrict, retire, mask in place - so the graph lane would otherwise never
    hear about them.
    """

    def update(self, **kwargs):
        graph = not GRAPH_FIELDS.isdisjoint(kwargs)
        passages = not CHUNK_FIELDS.isdisjoint(kwargs)
        if not graph and not passages:
            return super().update(**kwargs)
        touched = self.touched_applications() if graph else set()
        entry_ids = list(self.order_by().values_list("pk", flat=True)) if passages else []
        count = super().update(**kwargs)
        if count and passages:
            from ..triggers import passages_changed

            passages_changed(entry_ids, kwargs)
        if count and graph:
            moved = kwargs.get("application_id", kwargs.get("application"))
            if moved is not None:
                touched.add(getattr(moved, "pk", moved))
            from ..triggers import sources_changed

            sources_changed(touched)
        return count

    update.alters_data = True

    def delete(self):
        touched = self.touched_applications()
        result = super().delete()
        if result[0]:
            from ..triggers import sources_changed

            sources_changed(touched)
        return result

    delete.alters_data = True
    delete.queryset_only = True

    def touched_applications(self):
        return set(self.order_by().values_list("application_id", flat=True).distinct())

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
    #: The digest its `KnowledgeChunk` rows were cut from. Blank, or behind
    #: `digest`, means the passages are not written yet (`chunks.backfill`).
    chunked_digest = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["application", "active"])]


class KnowledgeChunk(models.Model):
    """One passage of an active source, as keyword search ranks it (`chunks.py`).

    Keyword search loaded up to a hundred whole sources per question and cut
    them into passages in Python, so a question against a library of large
    documents read megabytes to rank a few kilobytes. These rows are the same
    windows - `workbench.PASSAGE_LENGTH` long, `PASSAGE_STEP` apart - written in
    the transaction that writes the source, so the database can choose the
    passages and only those are read.

    A copy, never the record: citations still verify against
    `KnowledgeEntry.content`, and a passage is used only while its source's
    `chunked_digest` equals its `digest`. Written from the stored text, so it is
    already masked.
    """

    entry = models.ForeignKey(KnowledgeEntry, on_delete=models.CASCADE, related_name="chunks")
    #: Copied from the entry so a search is one index range, not a join first.
    application_id = models.UUIDField()
    ordinal = models.PositiveIntegerField()
    offset = models.PositiveIntegerField()
    #: The source's title, ranked with every passage as it always was.
    heading = models.CharField(max_length=200)
    text = models.TextField()
    digest = models.CharField(max_length=64)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["entry", "ordinal"], name="unique_chunk_position")
        ]
        indexes = [models.Index(fields=["application_id", "entry"])]


CONNECTOR_KINDS = [
    ("github", "GitHub"),
    ("jira", "Jira"),
    ("servicenow", "ServiceNow"),
    ("awx", "Ansible AWX"),
    ("rundeck", "Rundeck"),
    ("azure_automation", "Azure Automation"),
    ("automation_list", "Automation list"),
]


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
    #: Whether its sources, nodes and edges are also `GraphElement` rows (`graph_store`).
    stored = models.BooleanField(default=False)
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

    @classmethod
    def from_db(cls, db, field_names, values):
        """A version whose lists are held only as rows reads back whole.

        `graph_store.slim` removes an old version's sources, nodes and edges
        from its JSON once they are rows and read back exactly. Every reader of
        `data` still sees the whole version: it is put back together here, on
        load, from the rows - so nothing that reads a version had to change.
        """
        instance = super().from_db(db, field_names, values)
        data = instance.__dict__.get("data")
        if isinstance(data, dict) and data.get("slimmed"):
            from ..graph_store import inflate

            instance.data = inflate(instance, data)
        return instance


class GraphElement(models.Model):
    """One source, node or edge of a knowledge graph, for the versions it is in.

    See `graph_store`. Held once while it stays the same and keeps its place:
    version N is every row with `first_number <= N` and `last_number` empty or
    `>= N`, in `key` order, and reads back exactly as that version's JSON. A
    copy, never evidence: citations verify against the sources, as they always
    did. Edges also carry what ranking reads, so a question can be answered
    from the rows that hold its terms (`graph_store.RowsGraph`).
    """

    ELEMENTS = [("sources", "Source"), ("nodes", "Node"), ("edges", "Edge")]

    id = models.BigAutoField(primary_key=True)
    application = models.ForeignKey(Application, on_delete=models.CASCADE)
    element = models.CharField(max_length=8, choices=ELEMENTS)
    #: Its place in the version's list: sparse, so rows can be added between.
    key = models.PositiveBigIntegerField()
    digest = models.CharField(max_length=64)
    #: The source it came from, when it names one.
    knowledge_id = models.CharField(max_length=64, blank=True)
    body = models.JSONField()
    first_number = models.PositiveIntegerField()
    last_number = models.PositiveIntegerField(null=True, blank=True)
    # Edges only, as `graph_ai.prepare` derives them.
    start_node = models.CharField(max_length=64, blank=True)
    end_node = models.CharField(max_length=64, blank=True)
    text = models.TextField(blank=True)
    #: `ranking.terms(text)`, space-separated with a space at each end.
    terms = models.TextField(blank=True)
    length = models.PositiveIntegerField(default=0)
    #: The references it names that are not its own source's, padded likewise.
    names = models.TextField(blank=True)
    #: An AI relationship between two entities: what `graph_ai.walk` follows.
    typed = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["application", "element", "key", "first_number"],
                name="unique_graph_element",
            )
        ]
        indexes = [
            models.Index(
                fields=["application", "element", "last_number", "first_number"],
                name="graph_element_version",
            ),
            models.Index(fields=["application", "knowledge_id"], name="graph_element_source"),
            models.Index(fields=["application", "start_node"], name="graph_element_start"),
            models.Index(fields=["application", "end_node"], name="graph_element_end"),
        ]


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
