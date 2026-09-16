"""Application knowledge, evidence retrieval and immutable approval workflows."""

import hashlib
import json
import re
import threading
import uuid
from datetime import timedelta
from pathlib import Path

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.http import (
    Http404,
    HttpResponse,
    HttpResponseNotAllowed,
    JsonResponse,
    StreamingHttpResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .agent_runtime import streaming
from .agent_runtime.runtime import HISTORY_TURNS, HistoryTurn
from .models import (
    CHAT_MODES,
    AIConfiguration,
    ChangePlan,
    ChatConversation,
    ChatMessage,
    KnowledgeEntry,
)
from .policy import application_for
from .services import audit, feature_enabled


def access(user, pk, feature, write=False):
    app, grant = application_for(user, pk)
    if not feature_enabled(feature, app):
        raise PermissionDenied("This feature is disabled.")
    if write and grant.role not in {"owner", "contributor"}:
        raise PermissionDenied
    return app, grant


class QuestionForm(forms.Form):
    question = forms.CharField(
        max_length=2000,
        widget=forms.Textarea(
            attrs={"rows": 2, "placeholder": "Ask a question about your documents…"}
        ),
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
    """The old sources list, now the documents page under its own URL.

    The check runs here as well as in `documents` so a caller with no grant gets
    the same 404 from either URL, rather than one of them confirming the
    application exists by the shape of its refusal.
    """
    access(request.user, pk, "knowledge")
    if request.method == "POST":
        access(request.user, pk, "knowledge", write=True)
        return HttpResponseNotAllowed(["GET"])
    from .documents import documents

    return documents(request, pk)


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
    from .source_library import graph_usage

    return render(
        request,
        "knowledge_detail.html",
        {
            "application": app,
            "entry": entry,
            "versions": graph_usage(app, [entry]).get(str(entry.pk), []),
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


def conversation_context(conversation, app, user, before=None):
    """Recent exchanges, as provider-neutral HistoryTurn values.

    Returns plain values rather than model instances so a worker thread can carry
    history without holding an ORM object bound to a request.

    `before` excludes the tail a regenerate or edit is about to replace. Those
    messages still exist at this point - they are removed only once a replacement
    answer exists - so they have to be filtered out here or the model would be
    shown the very answer it is being asked to redo.
    """
    if not conversation:
        return []
    history = conversation.messages.filter(application=app, user=user)
    if before is not None:
        history = history.filter(sequence__lt=before)
    answers = list(
        history.filter(role="assistant", status="complete").order_by("-sequence")[:HISTORY_TURNS]
    )
    if not answers:
        return []
    questions = {
        message.sequence: message.body
        for message in history.filter(
            role="user", sequence__in=[answer.sequence - 1 for answer in answers]
        )
    }
    # Never resend excerpts/answers backed by a removed or changed source. Look up
    # only the cited sources rather than every source in the application.
    cited = {c.get("id") for answer in answers for c in answer.citations}
    active = (
        {
            str(e.pk): e.digest
            for e in KnowledgeEntry.objects.filter(
                application=app, active=True, pk__in=[c for c in cited if c]
            ).only("id", "digest")
        }
        if cited
        else {}
    )
    return [
        HistoryTurn(question=questions[answer.sequence - 1], answer=answer.body)
        for answer in reversed(answers)
        if answer.sequence - 1 in questions
        and all(active.get(c.get("id")) == c.get("digest") for c in answer.citations)
    ]


def lock_conversation(conversation):
    """Take the row lock the sequence allocator depends on.

    next_sequence reads the highest sequence and adds one, which is only safe
    while nothing else can do the same. The lock was documented but never
    actually taken: two concurrent sends could read the same value and both write
    it, violating the unique constraint after the provider had already been paid.
    SQLite serialises writers anyway, which is why the tests never showed it;
    PostgreSQL does not.
    """
    return ChatConversation.objects.select_for_update().filter(pk=conversation.pk).first()


def next_sequence(conversation):
    """Allocate the next message slot.

    Must be called inside a transaction that already holds the conversation row
    through lock_conversation, or two concurrent sends can claim one sequence.
    """
    last = conversation.messages.order_by("-sequence").values_list("sequence", flat=True).first()
    return 0 if last is None else last + 1


def record_exchange(app, user, conversation, question, answer, citations, mode, status="complete"):
    """Persist one question and its answer as two ordered messages."""
    sequence = next_sequence(conversation)
    ask = ChatMessage.objects.create(
        application=app,
        user=user,
        conversation=conversation,
        role="user",
        status="complete",
        body=question,
        mode=mode,
        sequence=sequence,
    )
    reply = ChatMessage.objects.create(
        application=app,
        user=user,
        conversation=conversation,
        role="assistant",
        status=status,
        body=answer,
        citations=citations,
        mode=mode,
        sequence=sequence + 1,
        finished_at=timezone.now(),
    )
    return ask, reply


def graph_version_choices(app_id):
    """Saved graph versions a conversation may be pinned to."""
    from .graph_ai import available_graph_versions

    return available_graph_versions(app_id)


def new_graph_version(request, app_id, graph_ai_enabled):
    """The graph version a conversation is pinned to at the moment it starts.

    A conversation freezes on the version it began with. Publishing a new graph
    afterwards must not change what an existing conversation answers from, or a
    thread silently becomes a mixture of two graphs and its earlier answers stop
    being reproducible. So when the user does not choose a version we pin the one
    published right now rather than leaving it open.
    """
    if not graph_ai_enabled:
        return None
    from .graph_ai import available_graph_versions

    raw = request.POST.get("graph_version", "")
    try:
        chosen = int(raw)
    except (TypeError, ValueError):
        chosen = None
    if chosen is not None and chosen in available_graph_versions(app_id):
        return chosen
    return published_graph_version(app_id)


def retention_days(app):
    """Days of chat history this application keeps; 0 means indefinitely."""
    from .models import ChatRetention

    record = ChatRetention.objects.filter(application=app).first()
    return record.days if record else ChatRetention.DEFAULT_DAYS


def purge_expired_conversations(app=None):
    """Delete conversations untouched for longer than the retention window.

    Keyed on updated_at, not created_at: a thread someone is still using is not
    stale. A window of 0 disables the purge for that application.
    """
    from .models import Application, ChatConversation, ChatMessage

    applications = [app] if app is not None else list(Application.objects.all())
    removed = 0
    for target in applications:
        days = retention_days(target)
        if not days:
            continue
        cutoff = timezone.now() - timedelta(days=days)
        stale = ChatConversation.objects.filter(application=target, updated_at__lt=cutoff)
        if not stale.exists():
            continue
        ChatMessage.objects.filter(conversation__in=stale).delete()
        removed += stale.delete()[0]
    return removed


def clear_conversations(user, app):
    """Delete every conversation this user holds in this application.

    Scoped to the one user: a conversation is private to whoever started it, so
    "clear all" must never reach another member's history.
    """
    from .models import ChatConversation, ChatMessage

    conversations = ChatConversation.objects.filter(application=app, user=user)
    count = conversations.count()
    ChatMessage.objects.filter(conversation__in=conversations).delete()
    conversations.delete()
    if count:
        audit(
            user,
            "chat.cleared",
            app.pk,
            app.product.portfolio.organization,
            details={"conversations": count},
        )
    return count


def published_graph_version(app_id):
    """The version graph answers use unless a conversation pins another."""
    from .graphs import published_revision

    revision = published_revision(app_id)
    return revision.number if revision else None


def credential_available(config, app):
    """Whether a run could authenticate, by mounted secret or development host login."""
    from .agent_runtime.credentials import host_login_enabled

    if (Path(settings.SECRET_DIRECTORY) / f"{config.provider}_{app.pk}").is_file():
        return True
    return config.provider == "claude" and host_login_enabled()


def mode_options(ai_enabled, graph_ai_enabled):
    """Every answer mode, each marked available or not.

    Unavailable modes are still listed rather than hidden. A capability that simply
    vanishes teaches nobody it exists; one that says "setup required" points at the
    thing to configure.
    """
    reasons = {
        "ai": "" if ai_enabled else "setup required",
        "graph": "" if graph_ai_enabled else "setup required",
        "search": "",
    }
    return [
        {
            "value": value,
            "label": label,
            "note": reasons.get(value, ""),
            "available": not reasons.get(value),
        }
        for value, label in CHAT_MODES
    ]


def available_modes(graph_ai_enabled):
    """Modes a conversation may be switched to. Search never needs configuration."""
    return [
        (value, label) for value, label in CHAT_MODES if value != "graph" or graph_ai_enabled
    ]


def unavailable_reason(mode):
    if mode == "graph":
        return (
            "Graph answers are not configured for this application. An application owner "
            "can enable Graph retrieval in AI settings."
        )
    if mode == "ai":
        return (
            "AI answers are not configured for this application. An application owner "
            "can enable Chat conversation in AI settings."
        )
    return "Choose an available answer mode."


def requested_mode(request, graph_ai_enabled):
    """Mode for a conversation that does not exist yet."""
    source = request.POST if request.method == "POST" else request.GET
    mode = source.get("mode", "ai")
    return mode if mode in dict(available_modes(graph_ai_enabled)) else "ai"


def wants_stream(request):
    """True when the caller asked for the streaming JSON contract."""
    return request.headers.get("X-Digital-Brain-Stream") == "1"


def failure_text(error):
    if isinstance(error, ValidationError):
        return " ".join(error.messages)
    return "AI credential is not configured. Ask an application owner to complete setup."


FOLLOWUP = re.compile(r"\b(it|that|those|they|them|this|more|continue|explain)\b", re.I)


def retrieval_query(question, history):
    """Carry topic terms into short/pronominal follow-ups; keep new topics independent."""
    if history and (FOLLOWUP.search(question) or len(question.split()) < 5):
        return question + " " + " ".join(turn.question for turn in history)
    return question


def lexical_citations(app, question):
    return [
        {"id": str(entry.pk), "title": entry.title, "digest": entry.digest, "excerpt": passage}
        for _, entry, passage in retrieve(app, question)
    ]


def answer_question(user, app, conversation, question, mode, graph_version=None, trim_from=None):
    """Produce and persist one exchange synchronously.

    This is the no-JavaScript path, and the fallback whenever streaming is
    unavailable. It raises rather than persisting a failed answer, so a provider
    error never leaves a half-finished turn in the transcript.

    `trim_from` is how regenerate and edit replace an exchange. The removal
    happens here, inside the same transaction as the new messages and only after
    the provider has answered - deleting first meant a provider failure took the
    question and every later message with it and returned nothing.
    """
    first_exchange = conversation is None
    history = conversation_context(conversation, app, user, before=trim_from)
    query = retrieval_query(question, history)
    citations = lexical_citations(app, query)
    answer = readable_evidence(citations)
    if mode in {"ai", "graph"}:
        from .ai import answer_with_ai, invoke_ai

        if mode == "graph":
            from .graph_ai import graph_citations

            citations = graph_citations(app.pk, query, version=graph_version)
            answer = invoke_ai(
                user, app.pk, "graph_retrieval", question, citations, history=history
            )
        else:
            answer = answer_with_ai(user, app.pk, question, citations, history=history)
    with transaction.atomic():
        access(user, app.pk, "chat")
        access(user, app.pk, "knowledge")
        if conversation is not None:
            lock_conversation(conversation)
        if trim_from is not None and conversation is not None:
            conversation.messages.filter(sequence__gte=trim_from).delete()
        if conversation is None:
            conversation = ChatConversation.objects.create(
                application=app,
                user=user,
                title=question[:120],
                mode=mode,
                graph_version=graph_version,
            )
        _, reply = record_exchange(app, user, conversation, question, answer, citations, mode)
        conversation.save(update_fields=["updated_at"])
        audit(user, "chat.answered", reply.pk, app.product.portfolio.organization)
    if first_exchange:
        retitle(user, app, reply.pk, question, answer)
    return conversation


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
    if request.method == "POST" and request.POST.get("action") == "clear":
        removed = clear_conversations(request.user, app)
        messages.success(
            request,
            f"Cleared {removed} conversation(s)." if removed else "You had no conversations.",
        )
        return redirect("chat", pk=pk)
    form = QuestionForm(request.POST or None)
    chat_config = AIConfiguration.objects.filter(application=app, purpose="chat").first()
    ai_enabled = bool(chat_config and chat_config.enabled)
    chat_key_present = bool(chat_config) and credential_available(chat_config, app)
    graph_ai_enabled = AIConfiguration.objects.filter(
        application=app, purpose="graph_retrieval", enabled=True
    ).exists()
    graph_versions = graph_version_choices(app.pk) if graph_ai_enabled else []
    from .graphs import published_revision, revision_drift

    published = published_revision(app.pk)
    published_graph = published.number if published else None
    # How far the revision answering here has moved from its sources. Shown,
    # never enforced: reproducibility is why answers come from a snapshot.
    published_changed, published_total = revision_drift(published, app.pk) if published else (0, 0)
    # Mode is a property of the conversation. A new conversation may be started in a
    # chosen mode; sending a message can never change it, so a paid mode is never
    # entered by accident.
    mode = conversation.mode if conversation else requested_mode(request, graph_ai_enabled)
    if request.method == "POST" and form.is_valid():
        if mode not in dict(available_modes(graph_ai_enabled)):
            form.add_error(None, unavailable_reason(mode))
        else:
            question = form.cleaned_data["question"]
            if mode in {"ai", "graph"} and wants_stream(request):
                # Streaming path: persist the exchange now and let the browser
                # open the event stream. The synchronous branch below stays the
                # no-JavaScript fallback and is never removed.
                conversation, placeholder = start_answer(
                    app,
                    request.user,
                    conversation,
                    question,
                    mode,
                    graph_version=new_graph_version(request, app.pk, graph_ai_enabled),
                )
                audit(
                    request.user,
                    "chat.answered",
                    placeholder.pk,
                    app.product.portfolio.organization,
                )
                return JsonResponse(
                    {
                        "conversation": str(conversation.pk),
                        "message": str(placeholder.pk),
                        "stream": reverse("chat-stream", args=[pk, placeholder.pk]),
                        "stop": reverse("chat-stop", args=[pk, placeholder.pk]),
                        "fragment": reverse("chat-message", args=[pk, placeholder.pk]),
                    },
                    status=201,
                )
            try:
                conversation = answer_question(
                    request.user,
                    app,
                    conversation,
                    question,
                    mode,
                    graph_version=(
                        conversation.graph_version
                        if conversation
                        else new_graph_version(request, app.pk, graph_ai_enabled)
                    ),
                )
                return redirect(f"{reverse('chat', args=[pk])}?conversation={conversation.pk}")
            except (ValidationError, ImproperlyConfigured) as error:
                form.add_error(None, failure_text(error))
    history_messages = (
        conversation.messages.filter(application=app, user=request.user).order_by(
            "sequence", "created_at", "id"
        )
        if conversation
        else ChatMessage.objects.none()
    )
    message_pages = Paginator(history_messages, 60)
    message_page = message_pages.get_page(
        request.GET.get("messages", message_pages.num_pages)
    )
    return render(
        request,
        "chat.html",
        {
            "application": app,
            "form": form,
            "conversation": conversation,
            "conversations": Paginator(conversations, 20).get_page(request.GET.get("history")),
            "message_page": message_page,
            "ai_enabled": ai_enabled,
            "chat_config": chat_config,
            "chat_key_present": chat_key_present,
            "grant": grant,
            "graph_ai_enabled": graph_ai_enabled,
            "mode": mode,
            "modes": mode_options(ai_enabled, graph_ai_enabled),
            "graph_versions": graph_versions,
            "retention_days": retention_days(app),
            "published_graph": published_graph,
            "published_changed": published_changed,
            "published_total": published_total,
        },
    )


def resend(request, pk, app, conversation, question, trim_from=None):
    """Re-ask a question, replacing the transcript from `trim_from` on success."""
    destination = f"{reverse('chat', args=[pk])}?conversation={conversation.pk}"
    try:
        answer_question(
            request.user,
            app,
            conversation,
            question,
            conversation.mode,
            graph_version=conversation.graph_version,
            trim_from=trim_from,
        )
    except (ValidationError, ImproperlyConfigured) as error:
        messages.error(request, failure_text(error))
    return redirect(destination)


def start_answer(app, user, conversation, question, mode, graph_version=None):
    """Persist the question and an empty assistant row, then hand back the placeholder.

    Written before the provider is contacted so the transcript has somewhere to
    stream into, and so a dropped connection still leaves a record of the ask.
    """
    with transaction.atomic():
        if conversation is None:
            conversation = ChatConversation.objects.create(
                application=app,
                user=user,
                title=question[:120],
                mode=mode,
                graph_version=graph_version,
            )
        else:
            lock_conversation(conversation)
        sequence = next_sequence(conversation)
        ChatMessage.objects.create(
            application=app,
            user=user,
            conversation=conversation,
            role="user",
            status="complete",
            body=question,
            mode=mode,
            sequence=sequence,
        )
        placeholder = ChatMessage.objects.create(
            application=app,
            user=user,
            conversation=conversation,
            role="assistant",
            status="streaming",
            body="",
            mode=mode,
            sequence=sequence + 1,
        )
        conversation.save(update_fields=["updated_at"])
    return conversation, placeholder


def finish_answer(message_id, status, body, citations, error="", provider="", model=""):
    """Single guarded write that closes out a streaming message.

    The status predicate makes this idempotent and makes a racing stop a no-op,
    which is what lets exactly one thread own the row.
    """
    return ChatMessage.objects.filter(pk=message_id, status="streaming").update(
        status=status,
        body=body,
        citations=citations,
        error=error[:300],
        provider=provider,
        model=model,
        finished_at=timezone.now(),
    )


def answer_worker(
    user_id, app_id, message_id, question, history, session, mode="ai", graph_version=None
):
    """Own the provider call and every database write for one streamed answer.

    Runs off the request thread, so it re-resolves its own objects and always
    releases its connection.

    `mode` and `graph_version` carry the conversation's own settings. Without
    them this path answered every conversation as ordinary chat over lexical
    search, including ones the page labelled "Graph answer" and ones pinned to a
    particular published version.
    """
    from django.db import close_old_connections

    from .ai import chat_configuration, stream_chat_answer
    from .models import User

    status, body, citations, error, provider, model = "failed", "", [], "", "", ""
    try:
        user = User.objects.get(pk=user_id)
        app, config, token = chat_configuration(user, app_id, mode=mode)
        provider, model = config.provider, config.model
        query = retrieval_query(question, history)
        if mode == "graph":
            from .graph_ai import graph_citations

            citations_seed = graph_citations(app.pk, query, version=graph_version)
        else:
            citations_seed = lexical_citations(app, query)
        body, citations = stream_chat_answer(
            user, app, config, token, question, citations_seed, history, session
        )
        status = "stopped" if session.stopped else "complete"
        session.emit("citations", {"citations": public_citations(citations)})
        if status == "complete":
            title = retitle(user, app, message_id, question, body)
            if title:
                session.emit("title", {"title": title})
    except (ValidationError, ImproperlyConfigured) as failure:
        error = failure_text(failure)
        session.emit("error", {"message": error})
    except (PermissionDenied, Http404):
        error = "Access to this application changed while the answer was in progress."
        session.emit("error", {"message": error})
    except Exception:
        error = "AI response unavailable."
        session.emit("error", {"message": error})
    finally:
        try:
            if status == "failed" and not body:
                finish_answer(message_id, "failed", "", [], error, provider, model)
            else:
                finish_answer(message_id, status, body, citations, error, provider, model)
            session.emit("done", {"status": status})
        finally:
            session.finish()
            close_old_connections()


def retitle(user, app, message_id, question, answer):
    """Replace the truncated first question with a generated name, once.

    Only for the opening exchange of a conversation the user has not named
    themselves. Any failure leaves the existing title alone.
    """
    from .ai import generate_title

    conversation = (
        ChatConversation.objects.filter(messages__id=message_id).distinct().first()
    )
    if conversation is None or conversation.title_locked:
        return None
    if conversation.messages.filter(role="assistant", status="complete").count() > 1:
        return None
    title = generate_title(user, app.pk, question, answer)
    if not title or title == conversation.title:
        return None
    ChatConversation.objects.filter(pk=conversation.pk, title_locked=False).update(title=title)
    return title


def public_citations(citations):
    """Citations as the browser may see them: never the digest."""
    return [
        {"id": c["id"], "title": c["title"], "excerpt": c["excerpt"]} for c in citations or []
    ]


@login_required
@require_http_methods(["GET"])
def chat_stream(request, pk, message_id):
    """Server-sent events for one assistant message.

    Authorization happens here, before the response is constructed: a permission
    error raised inside the generator would arrive after the 200 headers and
    truncate the body rather than returning 403.
    """
    app, _ = access(request.user, pk, "chat")
    access(request.user, pk, "knowledge")
    message = get_object_or_404(
        ChatMessage,
        pk=message_id,
        application=app,
        user=request.user,
        role="assistant",
        status="streaming",
    )
    history = conversation_context(message.conversation, app, request.user)
    question = (
        message.conversation.messages.filter(role="user", sequence=message.sequence - 1)
        .values_list("body", flat=True)
        .first()
    )
    if not question:
        raise Http404
    try:
        session = streaming.open_session(message.pk)
    except streaming.StreamCapacityError as full:
        return JsonResponse({"error": str(full)}, status=503)
    conversation = message.conversation
    threading.Thread(
        target=answer_worker,
        args=(
            request.user.pk,
            app.pk,
            message.pk,
            question,
            history,
            session,
            conversation.mode,
            conversation.graph_version,
        ),
        name=f"chat-answer-{message.pk}",
        daemon=True,
    ).start()
    response = StreamingHttpResponse(
        streaming.stream_frames(session), content_type="text/event-stream"
    )
    # No Content-Length, or waitress buffers the whole body instead of chunking.
    response["Cache-Control"] = "no-cache, no-transform"
    response["X-Accel-Buffering"] = "no"
    return response


@login_required
@require_http_methods(["POST"])
def chat_stop(request, pk, message_id):
    """Ask a running answer to stop. The worker still writes the row."""
    app, _ = access(request.user, pk, "chat")
    message = get_object_or_404(
        ChatMessage, pk=message_id, application=app, user=request.user, role="assistant"
    )
    if streaming.request_stop(message.pk):
        return JsonResponse({"status": "stopping"}, status=202)
    # No live stream in this process: close out an orphan left by a restart.
    finish_answer(message.pk, "stopped", message.body, message.citations)
    return JsonResponse({"status": "stopped"}, status=202)


@login_required
@require_http_methods(["GET"])
def chat_message(request, pk, message_id):
    """The server-rendered fragment for one finished message.

    The browser swaps this in when a stream ends so provider Markdown is only
    ever rendered by the trusted template filter, never by JavaScript.
    """
    app, _ = access(request.user, pk, "chat")
    message = get_object_or_404(ChatMessage, pk=message_id, application=app, user=request.user)
    return render(request, "_chat_message.html", {"message": message, "application": app})


def owned_conversation(user, app, conversation_id):
    return get_object_or_404(
        ChatConversation, pk=conversation_id, application=app, user=user
    )


@login_required
@require_http_methods(["POST"])
def chat_conversation(request, pk, conversation_id):
    """Rename, change mode, or delete one of the signed-in user's own conversations."""
    app, _ = access(request.user, pk, "chat")
    conversation = owned_conversation(request.user, app, conversation_id)
    action = request.POST.get("action")
    organization = app.product.portfolio.organization
    if action == "delete":
        with transaction.atomic():
            audit(request.user, "chat.conversation_deleted", conversation.pk, organization)
            conversation.delete()
        messages.success(request, "Conversation deleted.")
        return redirect("chat", pk=pk)
    if action == "rename":
        title = request.POST.get("title", "").strip()[:120]
        if not title:
            messages.error(request, "Enter a conversation name.")
        else:
            with transaction.atomic():
                conversation.title = title
                # A human name is never overwritten by a generated one.
                conversation.title_locked = True
                conversation.save(update_fields=["title", "title_locked", "updated_at"])
                audit(request.user, "chat.conversation_renamed", conversation.pk, organization)
    elif action == "graph-version":
        raw = request.POST.get("graph_version", "")
        if raw == "":
            conversation.graph_version = None
        else:
            from .graph_ai import available_graph_versions

            try:
                chosen = int(raw)
            except (TypeError, ValueError):
                chosen = None
            if chosen not in available_graph_versions(app.pk):
                messages.error(request, "Choose an available graph version.")
                return redirect(
                    f"{reverse('chat', args=[pk])}?conversation={conversation.pk}"
                )
            conversation.graph_version = chosen
        conversation.save(update_fields=["graph_version", "updated_at"])
    elif action == "mode":
        graph_ai_enabled = AIConfiguration.objects.filter(
            application=app, purpose="graph_retrieval", enabled=True
        ).exists()
        mode = request.POST.get("mode", "")
        if mode not in dict(available_modes(graph_ai_enabled)):
            messages.error(request, "Choose an available answer mode.")
        else:
            conversation.mode = mode
            conversation.save(update_fields=["mode", "updated_at"])
    else:
        raise Http404
    return redirect(f"{reverse('chat', args=[pk])}?conversation={conversation.pk}")


@login_required
@require_http_methods(["POST"])
def chat_regenerate(request, pk, message_id):
    """Discard an assistant answer and its question, then re-ask the same question."""
    app, _ = access(request.user, pk, "chat")
    access(request.user, pk, "knowledge")
    answer = get_object_or_404(
        ChatMessage, pk=message_id, application=app, user=request.user, role="assistant"
    )
    conversation = answer.conversation
    question = (
        conversation.messages.filter(role="user", sequence=answer.sequence - 1)
        .values_list("body", flat=True)
        .first()
    )
    if not question:
        raise Http404
    # The old exchange is removed by answer_question once a replacement exists.
    return resend(request, pk, app, conversation, question, trim_from=answer.sequence - 1)


@login_required
@require_http_methods(["POST"])
def chat_edit(request, pk, message_id):
    """Replace a question and drop everything after it, then re-ask."""
    app, _ = access(request.user, pk, "chat")
    access(request.user, pk, "knowledge")
    original = get_object_or_404(
        ChatMessage, pk=message_id, application=app, user=request.user, role="user"
    )
    conversation = original.conversation
    question = request.POST.get("question", "").strip()[:2000]
    if not question:
        messages.error(request, "Enter a question.")
        return redirect(f"{reverse('chat', args=[pk])}?conversation={conversation.pk}")
    return resend(request, pk, app, conversation, question, trim_from=original.sequence)


class DraftForm(forms.Form):
    requirement = forms.CharField(
        max_length=2000,
        widget=forms.Textarea(attrs={"rows": 3}),
        label="What change do you need?",
        help_text=(
            "An AI draft is filled into the form below for you to edit. "
            "Nothing is submitted for you."
        ),
    )


@login_required
@require_http_methods(["GET", "POST"])
def plans(request, pk):
    from .code_factory import ticket_choices
    from .models import Connector, FactoryRun

    app, grant = access(request.user, pk, "code_factory")
    form = PlanForm(request.POST or None)
    draft_form = DraftForm()
    draft_notice = ""
    plan_ai_enabled = AIConfiguration.objects.filter(
        application=app, purpose="plan_drafting", enabled=True
    ).exists()
    if request.method == "POST" and request.POST.get("action") == "analyse":
        from .code_factory import start_run

        access(request.user, pk, "code_factory", write=True)
        entry = get_object_or_404(
            KnowledgeEntry, pk=request.POST.get("ticket"), application=app, active=True
        )
        connector = Connector.objects.filter(
            pk=request.POST.get("connector"), application=app
        ).first()
        run = start_run(request.user, pk, entry, connector=connector)
        messages.success(
            request,
            f"Queued analysis of {entry.title}. Its phases appear below as they run.",
        )
        return redirect(f"{reverse('plans', args=[pk])}#run-{run.pk}")
    if request.method == "POST" and request.POST.get("action") == "draft":
        access(request.user, pk, "code_factory", write=True)
        access(request.user, pk, "knowledge")
        draft_form = DraftForm(request.POST)
        form = PlanForm()
        if draft_form.is_valid():
            requirement = draft_form.cleaned_data["requirement"]
            try:
                from .ai import draft_plan

                citations = lexical_citations(app, requirement)
                draft = draft_plan(request.user, pk, requirement, citations)
                form = PlanForm(
                    initial={
                        "title": draft["title"] or requirement[:200],
                        "proposal": draft["proposal"],
                        "validation": draft["validation"],
                    }
                )
                sources = ", ".join(c["title"] for c in draft["citations"])
                draft_notice = (
                    "Draft ready. Review and edit it before submitting. "
                    + (f"Evidence: {sources}. " if sources else "No source evidence was cited. ")
                    + (
                        f"{draft['rejected']} unverifiable quote(s) were dropped."
                        if draft["rejected"]
                        else ""
                    )
                )
                audit(
                    request.user,
                    "plan.drafted",
                    app.pk,
                    app.product.portfolio.organization,
                    details={"cited": len(draft["citations"])},
                )
            except (ValidationError, ImproperlyConfigured) as error:
                draft_form.add_error(None, failure_text(error))
    elif request.method == "POST":
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
            "draft_form": draft_form,
            "draft_notice": draft_notice,
            "plan_ai_enabled": plan_ai_enabled,
            "page": Paginator(ChangePlan.objects.filter(application=app), 20).get_page(
                request.GET.get("page")
            ),
            "runs": FactoryRun.objects.filter(application=app).prefetch_related("phases")[:10],
            "tickets": ticket_choices(app),
            "connectors": Connector.objects.filter(application=app, enabled=True),
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
    from .code_factory import confirm_repository, deliver, write_credential

    app, grant = access(request.user, pk, "code_factory")
    plan = get_object_or_404(ChangePlan, application=app, pk=plan_id)
    run = plan.runs.first()
    if request.method == "POST" and request.POST.get("action") == "confirm-repository":
        # The repository was guessed - from ticket text, or from this
        # application's registry when the ticket named nothing. Confirming it
        # is a person saying "yes, that one"; whoever can file a ticket does
        # not get to decide where this platform writes.
        access(request.user, pk, "code_factory", write=True)
        if run is None or not grant.can_approve:
            raise PermissionDenied
        try:
            confirmed = confirm_repository(
                run, request.POST.get("repository"), request.POST.get("base_branch")
            )
        except ValidationError as error:
            messages.error(request, " ".join(error.messages))
            return redirect("plan-detail", pk=pk, plan_id=plan_id)
        audit(
            request.user,
            "factory.repository_confirmed",
            run.pk,
            app.product.portfolio.organization,
            details={"repository": confirmed, "base_branch": run.base_branch},
        )
        messages.success(request, "Repository confirmed. Delivery can now be requested.")
        return redirect("plan-detail", pk=pk, plan_id=plan_id)
    if request.method == "POST" and request.POST.get("action") == "deliver":
        try:
            url = deliver(request.user, pk, run.pk if run else None)
        except ValidationError as error:
            messages.error(request, " ".join(error.messages))
        else:
            messages.success(request, f"Draft pull request opened: {url}")
        return redirect("plan-detail", pk=pk, plan_id=plan_id)
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
            "run": run,
            "can_deliver": bool(
                run
                and grant.can_approve
                and plan.status == "approved"
                and not run.pull_request_url
            ),
            "write_credential": bool(write_credential(app)),
        },
    )
