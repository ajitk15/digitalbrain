"""What a model call costs and may do: receipts, configurations, budgets, admissions."""

import uuid

from django.conf import settings
from django.db import models

from .accounts import Application


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
    #: Of `input_tokens` - every prompt token - those Anthropic's prompt cache
    #: served and those it wrote, each priced at its own rate (`ai.cost`). A
    #: `db_default`, so the release before still inserts receipts.
    cache_read_tokens = models.PositiveBigIntegerField(default=0, db_default=0)
    cache_write_tokens = models.PositiveBigIntegerField(default=0, db_default=0)
    #: The model turns this receipt bills. One for every provider request we can
    #: key; more for a Claude CLI run, which reports a whole run's usage once.
    #: Counting receipts used to read such a run as one turn.
    turns = models.PositiveIntegerField(default=1, db_default=1)
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


#: In the order AI settings lists them: the features people use every day first,
#: then the graph's own tasks, then the small housekeeping call.
AI_PURPOSES = [
    ("chat", "Chat conversation"),
    ("chat_search", "Chat searching"),
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
    ("knowledge_upkeep", "Knowledge upkeep"),
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


class AIAdmission(models.Model):
    """One paid model call in flight: its place among the gateway's slots, and
    the most it can cost, held against the application's monthly cap.

    Written by `gateway.admitted` before a call and deleted however it ends; a
    row a dead process left expires at `expires_at` and is swept. Never a record
    of what was said - that is the receipt's job, and only the receipt's.
    `application_id` is not a foreign key for the reason `Job`'s is not.
    """

    application_id = models.UUIDField()
    purpose = models.CharField(max_length=24)
    provider = models.CharField(max_length=24)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    reserved = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["application_id", "expires_at"], name="ai_admission_app"),
            models.Index(fields=["provider", "expires_at"], name="ai_admission_provider"),
        ]


AGENT_STEP_OUTCOMES = [
    ("found", "Found"),
    ("nothing", "Found nothing"),
    ("budget", "Over the tool budget"),
    ("stopped", "After Stop"),
    ("refused", "Access ended"),
    ("failed", "Failed"),
    ("repeat", "Already shown"),
]


class AgentStep(models.Model):
    """One tool call an agent run made (`agent_runtime.harness`), with no text in it.

    The tool's registry name, the *shape* of its arguments (numbers, ids and
    lengths - `agent_runtime.tools.shape`), what came of it, how much text came
    back, how many citations and how long it took. Never the query, the result
    or anything a model or a person wrote: it says how an answer was reached,
    not what it read. It belongs to the message it answered, or to the Code
    Factory run and phase it served, and goes with it, whatever removes that -
    its owner, chat retention, a reset, the application's deletion.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(Application, on_delete=models.CASCADE)
    # A string reference: `chat` is imported after this module. No constraint in
    # the database: the release before this one deletes messages without knowing
    # this table exists, and a constraint would refuse it while both run. Django
    # still cascades; a row the old release left behind is ids and counts only,
    # and goes with its application.
    message = models.ForeignKey(
        "platform_core.ChatMessage",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        db_constraint=False,
        related_name="agent_steps",
    )
    # Or the Code Factory run, and the phase of it, that the step served. No
    # constraint for the same reason as `message`.
    run = models.ForeignKey(
        "platform_core.FactoryRun",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        db_constraint=False,
        related_name="agent_steps",
    )
    # Or the ServiceOps triage run it served (Phase 3), the same way.
    triage = models.ForeignKey(
        "platform_core.TriageRun",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        db_constraint=False,
        related_name="agent_steps",
    )
    # Or the code review whose AI step it served (Phase 4), the same way.
    review = models.ForeignKey(
        "platform_core.CodeReview",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        db_constraint=False,
        related_name="agent_steps",
    )
    # Or the Learned question it drafted a fix for (Phase 6), the same way.
    candidate = models.ForeignKey(
        "platform_core.LearningCandidate",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        db_constraint=False,
        related_name="agent_steps",
    )
    # Or the code documentation whose AI step it served, the same way.
    docs = models.ForeignKey(
        "platform_core.CodeDocumentation",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        db_constraint=False,
        related_name="agent_steps",
    )
    # Or the knowledge source whose AI review it served, the same way.
    entry = models.ForeignKey(
        "platform_core.KnowledgeEntry",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        db_constraint=False,
        related_name="agent_steps",
    )
    phase = models.CharField(max_length=20, blank=True, default="", db_default="")
    purpose = models.CharField(max_length=24)
    sequence = models.PositiveIntegerField()
    tool = models.CharField(max_length=64)
    arguments = models.JSONField(default=dict, blank=True)
    outcome = models.CharField(max_length=10, choices=AGENT_STEP_OUTCOMES)
    result_characters = models.PositiveIntegerField(default=0)
    citations = models.PositiveIntegerField(default=0)
    duration_ms = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["message_id", "run_id", "sequence"]
        indexes = [
            models.Index(fields=["message", "sequence"], name="agent_step_message"),
            models.Index(fields=["run", "phase", "sequence"], name="agent_step_run"),
            models.Index(fields=["triage", "sequence"], name="agent_step_triage"),
            models.Index(fields=["review", "sequence"], name="agent_step_review"),
            models.Index(fields=["candidate", "sequence"], name="agent_step_candidate"),
            models.Index(fields=["docs", "sequence"], name="agent_step_docs"),
            models.Index(fields=["entry", "sequence"], name="agent_step_entry"),
        ]


#: What a measurement of an agent concluded (`agent_measurements.state_of`).
MEASUREMENT_VERDICTS = [
    ("clears", "Clears the bar"),
    ("no_better", "No better"),
    ("fails", "Does not clear"),
    ("incomplete", "Not compared"),
    ("too_few", "Too few to judge"),
]
#: How a measured run's calls were carried (`agent_measurements.carried_by`).
MEASUREMENT_RUNTIMES = [
    ("cli", "Claude CLI"),
    ("messages", "Messages API"),
    ("messages-cached", "Messages API, prompt cache"),
    ("openai", "OpenAI"),
]


class AgentMeasurement(models.Model):
    """One recorded comparison of an agent with tools against one call without
    (`agent_measurements`, written by a pack command's `--record`).

    Numbers only: the per-way totals the pack's own `metrics` worked out, the
    verdict its own `verdict` gave and the model that was measured - never a
    ticket, an incident, a finding or anything a model or a person wrote. The
    full record stays in the JSON file `file` names, under `benchmarks/`.
    Kept with its application, through a demonstration reset: it says how an
    agent did, not what the demo held.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(Application, on_delete=models.CASCADE)
    #: The pack that measured it (`agent_measurements.PACKS`) and the agent
    #: catalog key it measured.
    pack = models.CharField(max_length=24)
    agent = models.CharField(max_length=24)
    provider = models.CharField(max_length=80, blank=True, default="")
    model = models.CharField(max_length=160, blank=True, default="")
    #: The frozen pack's digest, so a new pack reads as a new reference.
    pack_digest = models.CharField(max_length=64, blank=True, default="")
    #: How its calls were carried (`agent_measurements.RUNTIMES`): the CLI, the
    #: Messages API with or without its prompt cache, or OpenAI. A different one
    #: is a new reference too - the same model costs differently through each.
    runtime = models.CharField(
        max_length=16, blank=True, default="", db_default="", choices=MEASUREMENT_RUNTIMES
    )
    metrics = models.JSONField(default=dict, blank=True)
    verdict = models.CharField(max_length=12, choices=MEASUREMENT_VERDICTS)
    verdict_text = models.CharField(max_length=300, blank=True, default="")
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    file = models.CharField(max_length=200, blank=True, default="")
    generated_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["generated_at"]
        indexes = [
            models.Index(
                fields=["application", "agent", "generated_at"], name="agent_measurement_by_agent"
            )
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["application", "pack", "generated_at", "model"],
                name="agent_measurement_once",
            )
        ]
