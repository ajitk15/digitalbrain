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
