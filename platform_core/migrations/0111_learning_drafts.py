"""Drafts for Learned questions (agentic plan, Phase 6).

Nullable columns throughout, so the release before writes candidates as it did:
the draft the agent proposed, who asked for it and when; agent steps linked to
the candidate they drafted for with no database constraint, as 0108-0110 did;
and the Knowledge upkeep task among AI settings' purposes.
"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("platform_core", "0110_agent_steps_for_reviews"),
    ]

    operations = [
        migrations.AddField(
            model_name="agentstep",
            name="candidate",
            field=models.ForeignKey(
                blank=True,
                db_constraint=False,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="agent_steps",
                to="platform_core.learningcandidate",
            ),
        ),
        migrations.AddField(
            model_name="learningcandidate",
            name="draft",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="learningcandidate",
            name="drafted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="learningcandidate",
            name="drafted_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="learning_drafts",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="aiconfiguration",
            name="purpose",
            field=models.CharField(
                choices=[
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
                    ("knowledge_upkeep", "Knowledge upkeep"),
                ],
                default="chat",
                max_length=24,
            ),
        ),
        migrations.AddIndex(
            model_name="agentstep",
            index=models.Index(
                fields=["candidate", "sequence"], name="agent_step_candidate"
            ),
        ),
    ]
