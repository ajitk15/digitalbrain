"""Chat: conversations and their messages."""

import uuid

from django.conf import settings
from django.db import models

from .accounts import Application

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
