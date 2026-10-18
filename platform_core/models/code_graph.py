"""Code Graph: repositories, snapshots, files, reviews and documentation."""

import uuid

from django.conf import settings
from django.db import models

from .accounts import Application


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
