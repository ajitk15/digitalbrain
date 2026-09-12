"""Application knowledge, evidence retrieval and immutable approval workflows."""

import hashlib
import json
import re
import uuid
from pathlib import Path

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .models import AIConfiguration, ChangePlan, ChatConversation, ChatTurn, KnowledgeEntry
from .policy import application_for
from .services import audit, feature_enabled


def access(user, pk, feature, write=False):
    app, grant = application_for(user, pk)
    if not feature_enabled(feature, app):
        raise PermissionDenied("This feature is disabled.")
    if write and grant.role not in {"owner", "contributor"}:
        raise PermissionDenied
    return app, grant


class KnowledgeForm(forms.Form):
    title = forms.CharField(max_length=200)
    content = forms.CharField(
        max_length=100000,
        widget=forms.Textarea(attrs={"rows": 7}),
        help_text="Plain text. Saved as a new immutable source; HTML is displayed as text.",
    )


class QuestionForm(forms.Form):
    question = forms.CharField(
        max_length=2000,
        widget=forms.Textarea(attrs={"rows": 2}),
        label="Ask your application knowledge",
    )


class PlanForm(forms.Form):
    title = forms.CharField(max_length=200)
    proposal = forms.CharField(
        max_length=20000,
        widget=forms.Textarea(attrs={"rows": 5}),
        help_text="Describe the requirement, proposed changes, affected files and risks.",
    )
    validation = forms.CharField(
        max_length=10000,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Acceptance criteria, tests and rollback steps.",
    )


@transaction.atomic
def add_knowledge(user, app_id, title, content, source="", document=None):
    app, _ = access(user, app_id, "knowledge", write=True)
    entry = KnowledgeEntry(
        application=app,
        author=user,
        title=title,
        content=content,
        source=source,
        document=document,
        digest=hashlib.sha256(content.encode()).hexdigest(),
    )
    entry.full_clean()
    entry.save()
    audit(user, "knowledge.created", entry.pk, app.product.portfolio.organization)
    return entry


def retrieve(app, question):
    # Bounded, deterministic lexical retrieval. No provider or semantic-search claims.
    terms = list(dict.fromkeys(re.findall(r"\w{3,}", question.lower())))[:20]
    terms = [
        t for t in terms if t not in {"the", "what", "how", "are", "does", "and", "for", "this"}
    ]
    if not terms:
        return []
    condition = Q()
    for term in terms:
        condition |= Q(content__icontains=term) | Q(title__icontains=term)
    entries = KnowledgeEntry.objects.filter(application=app, active=True).filter(condition)[:100]
    ranked = []
    for entry in entries:
        for offset in range(0, len(entry.content), 1200):
            passage = entry.content[offset : offset + 1400]
            words = set(re.findall(r"\w+", passage.lower()))
            score = sum(term in words for term in terms)
            if score:
                ranked.append((score, entry, passage))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked[:5]


@login_required
@require_http_methods(["GET", "POST"])
def knowledge(request, pk):
    app, grant = access(request.user, pk, "knowledge")
    form = KnowledgeForm(request.POST or None)
    if request.method == "POST":
        access(request.user, pk, "knowledge", write=True)
        if form.is_valid():
            add_knowledge(request.user, pk, **form.cleaned_data)
            messages.success(request, "Knowledge source added.")
            return redirect("knowledge", pk=pk)
    entries = KnowledgeEntry.objects.filter(application=app, active=True)
    query = request.GET.get("q", "").strip()[:200]
    if query:
        entries = entries.filter(Q(title__icontains=query) | Q(content__icontains=query))
    return render(
        request,
        "knowledge.html",
        {
            "application": app,
            "grant": grant,
            "form": form,
            "query": query,
            "page": Paginator(entries, 20).get_page(request.GET.get("page")),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def knowledge_detail(request, pk, entry_id):
    app, grant = access(request.user, pk, "knowledge")
    entry = get_object_or_404(KnowledgeEntry, pk=entry_id, application=app, active=True)
    if request.method == "POST":
        access(request.user, pk, "knowledge", write=True)
        with transaction.atomic():
            entry.active = False
            entry.save(update_fields=["active"])
            audit(request.user, "knowledge.archived", entry.pk, app.product.portfolio.organization)
        messages.success(request, "Source archived. New answers will exclude it.")
        return redirect("knowledge", pk=pk)
    return render(
        request,
        "knowledge_detail.html",
        {
            "application": app,
            "entry": entry,
            "grant": grant,
        },
    )


def readable_evidence(citations):
    if not citations:
        return (
            "No matching evidence was found in this application's knowledge. "
            "Try naming a document, system or topic, or add a relevant source in Knowledge."
        )
    parts = ["Here's what I found in your application's sources:"]
    for index, citation in enumerate(citations, 1):
        # Extractive fallback: preserve source wording rather than invent a synthesis.
        excerpt = citation["excerpt"].strip()
        if len(excerpt) > 650:
            excerpt = excerpt[:650].rsplit(" ", 1)[0] + "…"
        parts.append(f"**{citation['title']} [{index}]**\n\n{excerpt}")
    parts.append("These are source excerpts. Choose AI answer for a conversational explanation.")
    return "\n\n".join(parts)


def conversation_context(conversation, app, user):
    if not conversation:
        return []
    recent = list(
        conversation.turns.filter(application=app, user=user).order_by("-created_at", "-id")[:8]
    )
    # Never resend excerpts/answers backed by a removed or changed source.
    active = {
        str(e.pk): e.digest
        for e in KnowledgeEntry.objects.filter(application=app, active=True).only("id", "digest")
    }
    return [
        turn
        for turn in reversed(recent)
        if all(active.get(c.get("id")) == c.get("digest") for c in turn.citations)
    ]


@login_required
@require_http_methods(["GET", "POST"])
def chat(request, pk):
    app, grant = access(request.user, pk, "chat")
    access(request.user, pk, "knowledge")
    conversations = ChatConversation.objects.filter(application=app, user=request.user)
    selected_id = (
        request.POST.get("conversation")
        if request.method == "POST"
        else request.GET.get("conversation")
    )
    conversation = None
    if selected_id:
        try:
            selected_id = uuid.UUID(selected_id)
        except (ValueError, AttributeError):
            raise Http404 from None
        conversation = get_object_or_404(conversations, pk=selected_id)
    elif request.method == "GET" and request.GET.get("new") != "1":
        conversation = conversations.first()
    form = QuestionForm(request.POST or None)
    chat_config = AIConfiguration.objects.filter(application=app, purpose="chat").first()
    ai_enabled = bool(chat_config and chat_config.enabled)
    chat_key_present = bool(
        chat_config
        and (Path(settings.SECRET_DIRECTORY) / f"{chat_config.provider}_{app.pk}").is_file()
    )
    graph_ai_enabled = AIConfiguration.objects.filter(
        application=app, purpose="graph_retrieval", enabled=True
    ).exists()
    mode = request.POST.get("mode", "ai")
    if request.method == "POST" and form.is_valid():
        if mode not in {"search", "ai", "graph"}:
            form.add_error(None, "Choose an available answer mode.")
        else:
            question = form.cleaned_data["question"]
            history = conversation_context(conversation, app, request.user)
            # Carry topic terms into short/pronominal follow-ups; keep new topics independent.
            followup = bool(
                re.search(
                    r"\b(it|that|those|they|them|this|more|continue|explain)\b", question, re.I
                )
            )
            retrieval_question = question
            if history and (followup or len(question.split()) < 5):
                retrieval_question += " " + " ".join(t.question for t in history)
            evidence = retrieve(app, retrieval_question)
            citations = [
                {
                    "id": str(entry.pk),
                    "title": entry.title,
                    "digest": entry.digest,
                    "excerpt": passage,
                }
                for _, entry, passage in evidence
            ]
            answer = readable_evidence(citations)
            if mode in {"ai", "graph"}:
                from .ai import answer_with_ai, invoke_ai

                try:
                    if mode == "graph":
                        from .graph_ai import graph_citations

                        citations = graph_citations(pk, retrieval_question)
                        answer = invoke_ai(
                            request.user,
                            pk,
                            "graph_retrieval",
                            question,
                            citations,
                            history=history,
                        )
                    else:
                        answer = answer_with_ai(
                            request.user, pk, question, citations, history=history
                        )
                except (ValidationError, ImproperlyConfigured) as error:
                    text = (
                        "AI credential is not configured. "
                        "Ask an application owner to complete setup."
                    )
                    if isinstance(error, ValidationError):
                        text = " ".join(error.messages)
                    form.add_error(None, text)
            if not form.errors:
                with transaction.atomic():
                    access(request.user, pk, "chat")
                    access(request.user, pk, "knowledge")
                    if conversation is None:
                        conversation = ChatConversation.objects.create(
                            application=app, user=request.user, title=question[:120]
                        )
                    turn = ChatTurn.objects.create(
                        application=app,
                        user=request.user,
                        conversation=conversation,
                        question=question,
                        answer=answer,
                        citations=citations,
                        mode=mode,
                    )
                    conversation.save(update_fields=["updated_at"])
                    audit(
                        request.user, "chat.answered", turn.pk, app.product.portfolio.organization
                    )
                return redirect(f"{reverse('chat', args=[pk])}?conversation={conversation.pk}")
    turns = (
        conversation.turns.filter(application=app, user=request.user).order_by("created_at", "id")
        if conversation
        else ChatTurn.objects.none()
    )
    turn_pages = Paginator(turns, 30)
    turn_page = turn_pages.get_page(request.GET.get("messages", turn_pages.num_pages))
    return render(
        request,
        "chat.html",
        {
            "application": app,
            "form": form,
            "conversation": conversation,
            "conversations": Paginator(conversations, 20).get_page(request.GET.get("history")),
            "turn_page": turn_page,
            "ai_enabled": ai_enabled,
            "chat_config": chat_config,
            "chat_key_present": chat_key_present,
            "grant": grant,
            "graph_ai_enabled": graph_ai_enabled,
            "mode": mode,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def plans(request, pk):
    app, grant = access(request.user, pk, "code_factory")
    form = PlanForm(request.POST or None)
    if request.method == "POST":
        access(request.user, pk, "code_factory", write=True)
        if form.is_valid():
            with transaction.atomic():
                values = form.cleaned_data
                sources = list(
                    KnowledgeEntry.objects.filter(application=app, active=True).values(
                        "id", "digest"
                    )
                )
                sources = [{"id": str(s["id"]), "digest": s["digest"]} for s in sources]
                digest = hashlib.sha256(
                    json.dumps({**values, "sources": sources}, sort_keys=True).encode()
                ).hexdigest()
                plan = ChangePlan.objects.create(
                    application=app, author=request.user, sources=sources, digest=digest, **values
                )
                audit(request.user, "plan.submitted", plan.pk, app.product.portfolio.organization)
            return redirect("plan-detail", pk=pk, plan_id=plan.pk)
    return render(
        request,
        "plans.html",
        {
            "application": app,
            "grant": grant,
            "form": form,
            "page": Paginator(ChangePlan.objects.filter(application=app), 20).get_page(
                request.GET.get("page")
            ),
        },
    )


@transaction.atomic
def review_plan(user, app_id, plan_id, decision, note):
    app, grant = access(user, app_id, "code_factory")
    plan = get_object_or_404(ChangePlan.objects.select_for_update(), pk=plan_id, application=app)
    if not grant.can_approve or plan.author_id == user.pk:
        raise PermissionDenied("A different user with approval permission must review this plan.")
    if decision not in {"approved", "rejected"} or plan.status != "pending":
        raise ValidationError("This plan has already been reviewed or the decision is invalid.")
    if not note.strip() or len(note) > 2000:
        raise ValidationError("Enter a review note of up to 2,000 characters.")
    current = {
        str(e.pk): e.digest for e in KnowledgeEntry.objects.filter(application=app, active=True)
    }
    if decision == "approved" and any(current.get(s["id"]) != s["digest"] for s in plan.sources):
        raise ValidationError("A pinned source was archived or changed. Submit a fresh plan.")
    plan.status = decision
    plan.reviewed_by = user
    plan.reviewed_at = timezone.now()
    plan.review_note = note
    plan.save(update_fields=["status", "reviewed_by", "reviewed_at", "review_note"])
    audit(
        user,
        f"plan.{decision}",
        plan.pk,
        app.product.portfolio.organization,
        details={"digest": plan.digest},
    )
    return plan


@login_required
@require_http_methods(["GET", "POST"])
def plan_detail(request, pk, plan_id):
    app, grant = access(request.user, pk, "code_factory")
    plan = get_object_or_404(ChangePlan, application=app, pk=plan_id)
    if request.method == "POST":
        try:
            review_plan(
                request.user,
                pk,
                plan_id,
                request.POST.get("decision"),
                request.POST.get("note", ""),
            )
        except ValidationError as error:
            messages.error(request, " ".join(error.messages))
        return redirect("plan-detail", pk=pk, plan_id=plan_id)
    if request.GET.get("download") == "1":
        if plan.status != "approved":
            raise Http404
        payload = {
            "id": str(plan.pk),
            "application": str(app.pk),
            "title": plan.title,
            "proposal": plan.proposal,
            "validation": plan.validation,
            "sources": plan.sources,
            "digest": plan.digest,
            "review_note": plan.review_note,
        }
        response = HttpResponse(json.dumps(payload, indent=2), content_type="application/json")
        response["Content-Disposition"] = f'attachment; filename="plan-{plan.pk}.json"'
        return response
    return render(
        request,
        "plan_detail.html",
        {
            "application": app,
            "plan": plan,
            "can_review": grant.can_approve
            and plan.author_id != request.user.pk
            and plan.status == "pending",
        },
    )
