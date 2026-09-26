"""Application knowledge, evidence retrieval and immutable approval workflows."""

import hashlib
import json
import re
import threading
import uuid
from datetime import timedelta
from types import SimpleNamespace

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import models, transaction
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
from .model_catalog import MODEL_CHOICES
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
    from .secrets import source_of

    if source_of(app, config.provider):
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


def plans(request, pk):
    """Start a run, and see the ones this application has made.

    This used to carry a second way to make a change plan - a form somebody
    filled in, with an AI-drafted first attempt - and a list of every plan an
    application held. Neither survived contact with the pipeline: a plan is
    what a run produces, it is read on the run that produced it, and the list
    was a worse route to the same thing with no ticket and no stage on it.

    The drafting POST paths went with the list. They had no page posting to
    them, which is code claiming a capability the product does not offer.
    """
    from .code_factory import ticket_choices
    from .models import Connector, FactoryRun

    app, grant = access(request.user, pk, "code_factory")
    connectors = Connector.objects.filter(application=app, enabled=True)
    if request.method == "POST" and request.POST.get("action") == "analyse":
        from .code_factory import start_run

        access(request.user, pk, "code_factory", write=True)
        entry = get_object_or_404(
            KnowledgeEntry, pk=request.POST.get("ticket"), application=app, active=True
        )
        connector = Connector.objects.filter(
            pk=request.POST.get("connector"), application=app
        ).first()
        try:
            run = start_run(request.user, pk, entry, connector=connector)
        except ValidationError as error:
            # A run with nothing to cite is refused rather than queued, so this
            # is an answer to give the person now, not a 500.
            messages.error(request, " ".join(error.messages))
            return redirect("plans", pk=pk)
        messages.success(
            request,
            f"Queued analysis of {entry.title}. Its stages appear below as it runs.",
        )
        return redirect("run-detail", pk=pk, run_id=run.pk)
    if request.method == "POST" and request.POST.get("action") == "manual":
        return ticket_new(request, pk)
    if request.method == "POST":
        return HttpResponseNotAllowed(["GET"])
    runs_shown = list(
        FactoryRun.objects.filter(application=app).prefetch_related("phases", "events")[:5]
    )
    return render(
        request,
        "plans.html",
        {
            "application": app,
            "grant": grant,
            # Analysis is refused without one, so the form is not offered either:
            # a button that can only fail is worse than a sentence saying why.
            "published_graph": published_graph_version(app.pk),
            "runs": runs_shown,
            "more_runs": FactoryRun.objects.filter(application=app).count() > len(runs_shown),
            "active": any(run.in_flight for run in runs_shown),
            "tickets": ticket_choices(app),
            "connectors": connectors,
            # The queue an analysis runs against defaults to the connector that
            # imported most recently - the one whose tickets are on screen -
            # rather than to "any". With a single connector configured, "any" and
            # "that one" are the same choice, and the template drops it.
            "default_connector": connectors.order_by(
                models.F("last_synced_at").desc(nulls_last=True), "name"
            ).first(),
        },
    )
@login_required
@require_http_methods(["GET", "POST"])
def ticket_new(request, pk):
    """Enter a ticket by hand, on a page of its own.

    Opened as a popup from Code Factory by `modal.js`, which fetches this URL
    and lifts its `<main>`; with JavaScript off the button navigates here. The
    ticket is kept on the run alone and never becomes knowledge - see
    `code_factory.start_run`.
    """
    from .code_factory import NO_GRAPH, start_run

    app, _ = access(request.user, pk, "code_factory", write=True)
    published = published_graph_version(app.pk)
    title = request.POST.get("title", "")[:300]
    description = request.POST.get("description", "")
    error = "" if published else NO_GRAPH
    if request.method == "POST":
        try:
            run = start_run(request.user, pk, title=title, body=description)
        except ValidationError as refusal:
            error = " ".join(refusal.messages)
        else:
            messages.success(
                request,
                f"Queued analysis of {run.ticket_title}. Its stages appear below as it runs.",
            )
            return redirect("run-detail", pk=pk, run_id=run.pk)
    return render(
        request,
        "ticket_new.html",
        {
            "application": app,
            "published_graph": published,
            "title": title,
            "description": description,
            "error": error,
        },
    )


@login_required
@require_http_methods(["GET"])
def plan_item(request, pk, plan_id, item_id):
    """One gap, with the evidence behind it.

    A page rather than a panel because `modal.js` opens it by fetching this URL
    and lifting its `<main>`: there is no fragment mode, and with JavaScript off
    the link simply navigates here. Read-only - choosing whether to implement it
    is a decision about the whole plan and stays on the review form, where it is
    made once.
    """
    app, grant = access(request.user, pk, "code_factory")
    plan = get_object_or_404(ChangePlan, application=app, pk=plan_id)
    item = get_object_or_404(plan.items, pk=item_id)
    return render(
        request,
        "plan_item.html",
        {"application": app, "grant": grant, "plan": plan, "item": item},
    )


@login_required
@require_http_methods(["GET"])
def onboarding(request, pk):
    """What this application still needs, for whatever it is for.

    One page for Engineering, Operations or both: the shared connector choice,
    then each purpose's gates. Read-only, and every row links to the screen that
    fixes it rather than fixing it here: each of those screens has its own
    permission check and its own audit event, and a setup page that wrote to all
    of them would be a second way to do everything with none of that.
    """
    from .readiness import GATE_ICONS, GATES, gate_ready, outstanding, record_completion, setup
    from .services import purposes

    app, grant = access(request.user, pk, "knowledge")
    state = setup(app)
    record_completion(app, state)
    found = state.steps
    serving = purposes(app)
    # "Nothing but the switch that got you here" - derived from the application
    # rather than from a query parameter, so it still greets somebody who
    # created this last week and is only now coming back to it.
    fresh = not [
        step for step in found if step.ok and step.key not in {"code_factory", "service_ops"}
    ]
    # One sequence across both gates rather than 1-5 and then 1-4 again. The
    # number is what somebody says out loud - "I am stuck on step 6" - and two
    # sixes on one screen would make that ambiguous. Positional, so a step that
    # only appears once something is indexed does not renumber the ones above it.
    order = {step.key: index for index, step in enumerate(found, 1)}
    return render(
        request,
        "onboarding.html",
        {
            "application": app,
            "grant": grant,
            "fresh": fresh,
            "setup": state,
            "done": state.done,
            "total": state.total,
            "next_step": state.next_step,
            "next_number": order.get(getattr(state.next_step, "key", None)),
            "gates": [
                {
                    "key": key,
                    "label": label,
                    "icon": GATE_ICONS[key],
                    "blurb": blurb,
                    "steps": [
                        (order[step.key], step)
                        for step in found
                        if step.gate == key
                    ],
                    "ready": gate_ready(found, key),
                    "outstanding": outstanding(found, key),
                    "done": sum(1 for step in found if step.gate == key and step.ok),
                    "count": sum(1 for step in found if step.gate == key),
                }
                for key, label, blurb, purpose in GATES
                if purpose is None or purpose in serving
            ],
            "serving": serving,
        },
    )


@login_required
@require_http_methods(["GET"])
def runs(request, pk):
    """Every run this application has ever made, newest first.

    Separate from the Code Factory screen because that screen is for starting
    work and this one is for looking back at it: a run is kept for as long as
    the application is, and ten rows on a busy application is not a history.
    """
    from .models import FactoryRun

    app, grant = access(request.user, pk, "code_factory")
    page = Paginator(
        FactoryRun.objects.filter(application=app).prefetch_related("phases"), 20
    ).get_page(request.GET.get("page"))
    return render(
        request,
        "runs.html",
        {
            "application": app,
            "grant": grant,
            "page": page,
            "active": any(run.in_flight for run in page),
        },
    )


#: The agents of stage four, in order, with what each one is for. Named here
#: rather than read from the phase rows so a row exists before its phase does:
#: "not started yet" is the state somebody is looking at before they press the
#: button, and a missing row cannot say it.
AGENT_ROW = (
    ("work_order", "Work order", "Decides what each file must end up doing"),
    ("implementation", "Implementation", "Writes the new contents of each file"),
    ("tests", "Test author", "Writes the tests that prove the change"),
    ("review", "Change review", "Reads the finished files against the approved items"),
    ("verification", "Pre-write checks", "Re-reads every file before anything is written"),
    ("delivery", "Pull request", "Branches, commits and opens a draft"),
)


#: Each implementation agent in full, for the popup its row opens. The row keeps
#: the one-line version in AGENT_ROW. `model` says whether the agent calls one:
#: the last two are deterministic code, and naming a model for them would be
#: inventing a fact.
AGENT_EXPLAINED = {
    "work_order": {
        "does": "Reads every approved gap together and decides, for each file the "
        "change touches, what that file must end up doing and how to tell.",
        "reads": "The approved gaps, and the current contents of each file they name.",
        "produces": "One intent and a short list of checks per file, plus any approved "
        "gap that no file could satisfy.",
        "never": "Writes code. It only states what each file is for.",
        "model": True,
    },
    "implementation": {
        "does": "Writes the new contents of each file, one file at a time, against the "
        "intent the work order set for it.",
        "reads": "The work order, the approved gaps, each file as it is now, and - "
        "read-only - the code those files import and the modules the gaps name, so "
        "it calls existing code as it really is.",
        "produces": "A complete new version of every file it changes or creates.",
        "never": "Touches the repository. The files are held here until you publish.",
        "model": True,
    },
    "tests": {
        "does": "Writes the tests that prove the change. It uses the test files the "
        "approved gaps name; when they name none, it picks one per changed source file "
        "from the repository's own layout, extending an existing test where there is one.",
        "reads": "The approved gaps, the files the implementation agent wrote, any "
        "existing test file it is extending, and which test framework and CI the "
        "repository is configured with - so new tests use what is already there.",
        "produces": "New or updated test files.",
        "never": "Runs the tests. The repository's own CI does, once a pull request exists.",
        "model": True,
    },
    "review": {
        "does": "Reads each finished file against the approved gap and intent it was "
        "written for, and rejects anything that falls short or goes further.",
        "reads": "The work order and every file the other agents wrote.",
        "produces": "A keep or reject decision per file, with the reason for each rejection.",
        "never": "Edits a file. A rejected file is simply left out of the change.",
        "model": True,
    },
    "verification": {
        "does": "Re-reads every file just before anything is written, so nothing lands "
        "on top of a change somebody else made in the meantime.",
        "reads": "Each target file as it is now, compared with the version the agents read.",
        "produces": "A list of the files checked, and the new paths confirmed still absent.",
        "never": "Calls a model. These are fixed checks: paths, elisions and staleness.",
        "model": False,
    },
    "delivery": {
        "does": "Creates a branch, commits the kept files and opens a draft pull request.",
        "reads": "The files that survived review, and the confirmed repository and branch.",
        "produces": "A branch and a draft pull request. Nothing is merged.",
        "never": "Calls a model, or runs anything without your Yes on the summary.",
        "model": False,
    },
}

#: "claude-sonnet-5" as people say it, from the same catalogue AI settings offers.
MODEL_LABELS = {
    value.split(":", 1)[1]: label
    for _, group in MODEL_CHOICES
    for value, label in group
    if ":" in value
}


def agent_model(name, phase, configured):
    """The model an agent used, or would use, and which of the two this is.

    Recorded on the phase once it has run, because the setting can change
    afterwards; before that, the application's Code Factory model, since every
    model-backed agent asks through the `plan_drafting` configuration.
    """
    if not AGENT_EXPLAINED[name]["model"]:
        return {"label": "No model", "source": "deterministic", "id": ""}
    if phase is not None and phase.model:
        return {
            "label": MODEL_LABELS.get(phase.model, phase.model),
            "source": "used",
            "id": f"{phase.provider}:{phase.model}" if phase.provider else phase.model,
        }
    if configured is not None:
        return {
            "label": MODEL_LABELS.get(configured.model, configured.model),
            "source": "configured",
            "id": f"{configured.provider}:{configured.model}",
        }
    return {"label": "No model configured", "source": "missing", "id": ""}


#: What an agent's failure means, for someone who is not reading the code.
#: Keyed on the start of the recorded error, which `code_factory` writes.
FAILURE_EXPLAINED = (
    (
        "did not return usable JSON",
        "The agent must answer in a fixed, machine-readable format (JSON) so its "
        "answer can be checked before anything is used. This reply could not be read "
        "in that format, so it was set aside and nothing was written. It is usually a "
        "one-off slip by the model; rerunning normally works.",
    ),
    (
        "returned no usable file changes",
        "The agent answered, but gave back no file it had actually changed, so "
        "nothing was written. When it says why above, that reason is the model's "
        "own; the usual one is that the change depends on a file it was not shown, "
        "which a plan naming that file fixes.",
    ),
    (
        "did not return a JSON object",
        "The agent answered in JSON, but not in the shape it was asked for, so the "
        "answer was set aside and nothing was written. Rerunning normally works.",
    ),
)


def failure_explained(error):
    for fragment, explanation in FAILURE_EXPLAINED:
        if fragment in (error or ""):
            return explanation
    return ""


def analysis_view(run, phases_by_name, items):
    """The Analysis stage as results first and log second.

    It used to be the run log alone - about twenty timestamped lines where
    "Code Factory switched on" and "Gap analysis found 7 item(s)" looked the
    same. Everything here is read from what the phases already recorded, so the
    cards cannot say more than the receipt does.
    """
    from .code_factory import AGENTS, BUILD_A, BUILD_B, language_gap
    from .code_graph_analysis import describe_languages
    from .models import ITEM_CATEGORIES

    triage_phase = phases_by_name.get("triage")
    triage = (triage_phase.output or {}) if triage_phase and triage_phase.status == "ok" else {}
    analysis_phase = phases_by_name.get("analysis")
    snapshot = run.code_snapshot
    languages = getattr(snapshot, "languages", None) or []
    counts = {}
    for item in items:
        counts[item.category] = counts.get(item.category, 0) + 1
    steps = []
    for name in BUILD_A:
        phase = phases_by_name.get(name)
        steps.append(
            {
                "label": AGENTS.get(name, name),
                "status": phase.status if phase else "pending",
                "model": MODEL_LABELS.get(phase.model, phase.model) if phase else "",
                "seconds": (
                    round(phase.duration_ms / 1000, 1) if phase and phase.duration_ms else None
                ),
                "tokens": (
                    f"{phase.prompt_tokens:,} in / {phase.completion_tokens:,} out"
                    if phase and phase.prompt_tokens
                    else ""
                ),
                "error": (phase.error or "")[:200] if phase else "",
            }
        )
    # Analysis is everything before implementation was asked for. Later
    # problems - a review that failed, a CI result - belong to later stages,
    # and listing them here made the analysis read as if it had gone wrong.
    events = list(run.events.all())
    later = next(
        (
            index
            for index, event in enumerate(events)
            if event.phase in BUILD_B or event.message.startswith("Implementation requested")
        ),
        len(events),
    )
    events = events[:later]
    return {
        "triage": triage,
        "evidence": {
            "graph_version": run.graph_version,
            "verified": analysis_phase.citations_verified if analysis_phase else 0,
            "rejected": analysis_phase.citations_rejected if analysis_phase else 0,
            "snapshot": snapshot,
            "languages": describe_languages(languages) if languages else "",
        },
        "language_gap": language_gap(snapshot) if snapshot else "",
        "gaps": [
            {"key": key, "label": label, "count": counts[key]}
            for key, label in ITEM_CATEGORIES
            if counts.get(key)
        ],
        "gap_total": len(items),
        "steps": steps,
        "problems": [event for event in events if event.level == "problem"],
    }


def tests_unrun(phase):
    """The sentence for a change whose new tests nothing will run, or ""."""
    from .repo_testing import ci_summary

    if phase is None or phase.status != "ok":
        return ""
    setup = (phase.output or {}).get("setup") or {}
    if setup.get("known") and not setup.get("ci"):
        return ci_summary(setup)
    return ""


def code_factory_model(app):
    return AIConfiguration.objects.filter(
        application=app, purpose="plan_drafting", enabled=True
    ).first()


def agent_report(name, label, waiting, phase, run, configured=None):
    """One agent's row: what it is for, or what it actually did.

    A finished agent should say what happened in *this* run rather than repeat
    its job description - "Passed" is true of every successful check and tells
    a reader nothing. Each one reports from its own recorded output, which is
    the same output the phase receipt is built from.
    """
    if phase is None:
        # A run that has already been through this stage and has no row for an
        # agent never had that agent: it predates it. Saying "waiting" about
        # something that was never going to happen is worse than saying so.
        # The pull request is the one agent a prepared run has not reached yet
        # by design: it waits for a Yes on the summary, so it is waiting, not
        # missing.
        awaiting_yes = name == "delivery" and run.status == "prepared"
        past = run.status in {"prepared", "delivered", "complete"} and not awaiting_yes
        if awaiting_yes:
            waiting = "Waiting for your Yes on the summary below. Nothing is written until then."
        return {
            "name": name,
            "label": label,
            "detail": "Did not run: this run predates this agent." if past else waiting,
            "status": "absent" if past else "pending",
            "model": agent_model(name, None, configured),
        }
    output = phase.output or {}
    detail = waiting
    explanation = ""
    if phase.status == "failed":
        detail = phase.error[:200] or "Failed, with no reason recorded."
        explanation = failure_explained(phase.error)
    elif phase.status == "skipped":
        detail = output.get("reason", "Nothing for it to do.")
    elif phase.status == "running":
        detail = "Working."
    elif phase.status == "ok":
        if name == "work_order":
            order, leftover = output.get("order", {}), output.get("leftover", [])
            detail = f"Set an intent for {len(order)} file(s)"
            detail += (
                f"; {len(leftover)} approved item(s) no file could satisfy."
                if leftover
                else "."
            )
        elif name == "implementation":
            files = output.get("files", [])
            created = [item for item in files if item.get("new")]
            named = ", ".join(item.get("path", "") for item in files[:3])
            detail = f"Wrote {len(files)} file(s)"
            detail += f", {len(created)} of them new" if created else ""
            detail += f": {named}" + ("…" if len(files) > 3 else "") + "."
        elif name == "tests":
            from .repo_testing import ci_summary

            files = output.get("files", [])
            detail = f"Wrote {len(files)} test file(s): {', '.join(files[:3])}."
            setup = output.get("setup") or {}
            chosen = setup.get("python") or setup.get("javascript")
            if chosen:
                detail += f" Framework: {chosen}."
            if setup.get("known") and not setup.get("ci"):
                detail += f" {ci_summary(setup)}"
        elif name == "review":
            kept, rejected = output.get("kept", []), output.get("rejected", [])
            detail = f"Passed {len(kept)} file(s)"
            if rejected:
                first = rejected[0]
                detail += (
                    f"; rejected {len(rejected)}, including {first.get('path', '')}"
                    f" — {first.get('reason', 'no reason given')}"
                )
            orphaned = output.get("orphaned", [])
            if orphaned:
                detail += (
                    f"; dropped {len(orphaned)} test file(s) written for rejected code: "
                    + ", ".join(item.get("path", "") for item in orphaned[:3])
                )
            detail += "."
        elif name == "verification":
            checked, created = output.get("checked", []), output.get("created", [])
            detail = f"Re-read {len(checked)} file(s); none had moved"
            detail += (
                f", and {len(created)} new path(s) were still absent." if created else "."
            )
        elif name == "delivery":
            detail = (
                f"Committed to {output.get('branch', 'a branch')} and opened a draft "
                "pull request."
            )
    if phase.prompt_tokens:
        detail += f" ({phase.prompt_tokens:,} in / {phase.completion_tokens:,} out tokens)"
    return {
        "name": name,
        "label": label,
        "detail": detail,
        "explanation": explanation,
        "status": phase.status,
        "model": agent_model(name, phase, configured),
    }


@login_required
@require_http_methods(["GET", "POST"])
def run_agent(request, pk, run_id, name):
    """One implementation agent of one run: what it is for and what it did.

    A page rather than a panel, because `modal.js` opens it by fetching this URL
    and lifting its `<main>`; with JavaScript off the link simply navigates
    here. Read-only. Everything shown is what the phase recorded - its output
    is model text, so the template escapes it like any other.
    """
    from .code_factory import request_preparation
    from .models import FactoryRun

    app, grant = access(request.user, pk, "code_factory")
    run = get_object_or_404(
        FactoryRun.objects.select_related("plan"), pk=run_id, application=app
    )
    labels = {key: label for key, label, _ in AGENT_ROW}
    if name not in labels:
        raise Http404
    phase = run.phases.filter(name=name).first()
    output = (phase.output or {}) if phase else {}
    # Rerunning an agent is rerunning the implementation: the agents feed one
    # another, so one of them alone would be working from a stale input. The
    # pull request is not offered here - it writes to somebody's repository,
    # and that decision belongs on the summary where the Yes is asked for.
    can_rerun = bool(
        phase
        and phase.status == "failed"
        and run.status == "failed"
        and name != "delivery"
        and grant.can_approve
        and run.plan
        and run.plan.status == "approved"
        and not run.pull_request_url
    )
    if request.method == "POST":
        try:
            if not can_rerun:
                raise ValidationError("This agent cannot be rerun from here.")
            request_preparation(request.user, pk, run.pk)
        except ValidationError as error:
            messages.error(request, " ".join(error.messages))
        else:
            messages.success(
                request, "The implementation agents are running again. Watch them below."
            )
        return redirect(f"{reverse('run-detail', args=[pk, run.pk])}#stage-4")
    return render(
        request,
        "run_agent.html",
        {
            "application": app,
            "run": run,
            "name": name,
            "label": labels[name],
            "explained": AGENT_EXPLAINED[name],
            "phase": phase,
            "model": agent_model(name, phase, code_factory_model(app)),
            "output": output,
            "order": sorted((output.get("order") or {}).items()),
            "events": run.events.filter(phase=name).order_by("sequence"),
            "can_rerun": can_rerun,
            "explanation": failure_explained(phase.error) if phase else "",
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def run_detail(request, pk, run_id):
    """One run, step by step, while it happens and afterwards.

    The events are the narration and the phases are the receipt; both are shown
    because they answer different questions. Read-only on purpose: approving the
    plan and confirming the repository are decisions with their own gates, and
    this page links to them rather than growing a second copy of either.
    """
    from . import code_factory
    from .code_factory import (
        REFRESH_TARGETS,
        confirm_repository,
        decline_refresh,
        discard,
        publish,
        refresh_after,
        request_preparation,
        write_credential,
    )
    from .models import FactoryRun
    from .readiness import setup

    app, grant = access(request.user, pk, "code_factory")
    run = get_object_or_404(
        FactoryRun.objects.select_related("plan", "code_snapshot"), pk=run_id, application=app
    )
    if request.method == "POST":
        access(request.user, pk, "code_factory", write=True)
        action = request.POST.get("action")
        try:
            if action == "review" and run.plan:
                review_plan(
                    request.user,
                    pk,
                    run.plan.pk,
                    request.POST.get("decision"),
                    request.POST.get("note", ""),
                    chosen=request.POST.getlist("item"),
                    declared=bool(request.POST.get("items_declared")),
                )
                messages.success(request, "Decision recorded.")
            elif action == "confirm-repository":
                if not grant.can_approve:
                    raise PermissionDenied
                confirm_repository(
                    run, request.POST.get("repository"), request.POST.get("base_branch")
                )
                messages.success(request, "Repository confirmed.")
            elif action == "retry-analysis":
                code_factory.retry_analysis(request.user, pk, run.pk)
                messages.success(request, "Analysis queued again. Its stages appear below.")
            elif action == "prepare":
                # Queued for the worker rather than run here: it is several
                # model calls and a series of reads, and holding the request
                # open for it would mean a blank page with nothing to report.
                request_preparation(request.user, pk, run.pk)
                messages.success(
                    request, "The implementation agents are running. Watch them below."
                )
            elif action == "publish":
                url = publish(request.user, pk, run.pk)
                messages.success(request, f"Draft pull request opened: {url}")
            elif action == "discard":
                discard(request.user, pk, run.pk)
                messages.success(request, "The prepared change was discarded. Nothing was written.")
            elif action == "refresh":
                for line in refresh_after(
                    request.user, pk, run.pk, request.POST.getlist("target")
                ):
                    messages.success(request, line)
            elif action == "refresh-decline":
                decline_refresh(request.user, pk, run.pk)
                messages.success(
                    request,
                    "Recorded. This application's documents and graphs are left as "
                    "they are, and this run stops asking.",
                )
            else:
                raise ValidationError("Unknown action.")
        except ValidationError as error:
            messages.error(request, " ".join(error.messages))
        return redirect("run-detail", pk=pk, run_id=run.pk)

    checks = [
        step
        for step in setup(app).steps
        if step.gate == "analysis" or step.key == "github_write"
    ]
    phases_by_name = {phase.name: phase for phase in run.phases.all()}
    items = list(run.plan.items.order_by("sequence")) if run.plan else []
    analysis = analysis_view(run, phases_by_name, items)
    if run.code_snapshot:
        # A pre-check the run itself can answer: whether the code it pinned is
        # code it can see the structure of.
        checks.append(
            SimpleNamespace(
                ok=not analysis["language_gap"],
                label="Every code language parsed"
                if not analysis["language_gap"]
                else "Some code is in a language Code Graph does not parse",
            )
        )
    configured = code_factory_model(app)
    agents = [
        agent_report(name, label, waiting, phases_by_name.get(name), run, configured)
        for name, label, waiting in AGENT_ROW
    ]
    # How long since the run last said anything. A working run that has gone
    # quiet is worth showing before the reclaimer decides it is dead: a spinner
    # that means nothing is worse than a number that means something.
    quiet_for = None
    if run.status in code_factory.WORKING:
        quiet_for = int(
            (timezone.now() - code_factory.last_sign_of_life(run)).total_seconds() // 60
        )
    plan = run.plan
    reviewable = bool(
        plan
        and plan.status == "pending"
        and grant.can_approve
        and (plan.author_id != request.user.pk or self_approval_allowed())
    )
    return render(
        request,
        "run_detail.html",
        {
            "application": app,
            "grant": grant,
            "run": run,
            "events": run.events.all(),
            "phases": run.phases.all(),
            "items": plan.items.order_by("sequence") if plan else (),
            "active": run.in_flight,
            # Each stage section's state, keyed by its number as a string so the
            # template can say `stage_state.3`. Read from `run.stages`, the same
            # list the header strip and every run list render.
            "stage_state": {str(stage["number"]): stage["state"] for stage in run.stages},
            # A run that stopped before producing a plan failed in analysis, and
            # can be queued again on the same record.
            "can_retry_analysis": bool(
                run.status == "failed"
                and run.plan is None
                and grant.role in {"owner", "contributor"}
            ),
            "failed_agent": next(
                (agent for agent in agents if agent["status"] == "failed"), None
            ),
            # Section one: the gate, as green ticks rather than a page.
            "checks": checks,
            "checks_passed": sum(1 for step in checks if step.ok),
            "accepted_count": plan.items.exclude(status="rejected").count() if plan else 0,
            "agents": agents,
            "quiet_for": quiet_for,
            "stall_after": int(code_factory.STALL_AFTER.total_seconds() // 60),
            "reviewable": reviewable,
            "changes": run.changes.all() if run.status in {"prepared", "delivered"} else (),
            # Said on the summary, before anyone opens a pull request: tests that
            # nothing runs are not evidence, and a green PR would not say so.
            "tests_unrun": tests_unrun(phases_by_name.get("tests")),
            "analysis": analysis,
            "can_deliver": bool(
                plan
                and plan.status == "approved"
                and grant.can_approve
                and not run.pull_request_url
            ),
            "write_credential": bool(write_credential(app)),
            # Offered once the change exists and its tests have stopped moving.
            # The stage itself decides when to show; this is only the vocabulary
            # it renders, so the list and the wording live in one place.
            "refresh_targets": REFRESH_TARGETS,
            "refresh_stage": next(
                (stage for stage in run.stages if stage["number"] == 7), None
            ),
        },
    )


def self_approval_allowed():
    """Whether one person may approve a plan they wrote.

    Off unless a deployment writes it down. Separation of duties is still the
    default and still the shape of the product: two people, one who asks and one
    who agrees. But a single-operator instance has nobody else to ask, and
    refusing this outright there left the pipeline unrunnable rather than
    strict - so it is now a deployment's decision in every mode, production
    included, instead of a development-only concession.

    What did not change is that it is never silent. `ChangePlan` records the
    approver, so a plan approved by its author says so in the audit record; and
    `review_plan` still refuses
    outright when the setting is off.
    """
    from django.conf import settings

    return bool(getattr(settings, "ALLOW_SELF_APPROVAL", False))


@transaction.atomic
def review_plan(user, app_id, plan_id, decision, note, chosen=None, declared=False):
    """Approve or reject a plan, and say which of its items are in.

    Choosing items is part of reviewing rather than a step beside it: the
    reviewer is deciding what will be built, and deciding it once - at the same
    moment, in the same submission - is what keeps "what was approved" and "what
    was delivered" the same set. Nothing can change it afterwards, because a
    plan is only reviewable while it is pending.

    `declared` distinguishes "none of them" from "this caller said nothing about
    items", for the reason the features form has the same marker: an unticked
    checkbox is simply absent from a POST, and without the marker a reviewer who
    ticked nothing and a caller that has never heard of items look identical.
    Without it every item stands, which is what happened before this existed.
    """
    app, grant = access(user, app_id, "code_factory")
    plan = get_object_or_404(ChangePlan.objects.select_for_update(), pk=plan_id, application=app)
    if not grant.can_approve or (plan.author_id == user.pk and not self_approval_allowed()):
        raise PermissionDenied("A different user with approval permission must review this plan.")
    if decision not in {"approved", "rejected"} or plan.status != "pending":
        raise ValidationError("This plan has already been reviewed or the decision is invalid.")
    if not note.strip() or len(note) > 2000:
        raise ValidationError("Enter a review note of up to 2,000 characters.")
    selected = set(chosen or ())
    if decision == "approved" and declared and not selected:
        # Approving nothing is not an approval. Saying so beats writing a plan
        # whose every item is rejected and then failing at implementation with
        # "none of the files the design named could be read".
        raise ValidationError(
            "Choose at least one item to implement, or reject the plan instead."
        )
    current = {
        str(e.pk): e.digest for e in KnowledgeEntry.objects.filter(application=app, active=True)
    }
    if decision == "approved":
        # A source entry with no digest cannot be re-checked, so it cannot be
        # approved: refused in the same words rather than raising KeyError,
        # which is what a plan written before the digest was recorded did.
        if any("digest" not in source for source in plan.sources):
            raise ValidationError(
                "This plan did not record the digest of its evidence, so it "
                "cannot be verified. Submit a fresh plan."
            )
        if any(current.get(s["id"]) != s["digest"] for s in plan.sources):
            raise ValidationError("A pinned source was archived or changed. Submit a fresh plan.")
    accepted = plan.items.count()
    if declared:
        # Recorded per item rather than as a list on the plan, because every
        # consumer already asks the item: target_paths, the implementation
        # prompt and the pull request body all exclude a rejected one.
        plan.items.filter(pk__in=selected).update(status="accepted")
        plan.items.exclude(pk__in=selected).update(status="rejected")
        accepted = len(selected)
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
        details={
            "digest": plan.digest,
            "items_accepted": accepted,
            "items_total": plan.items.count(),
            # Whoever reads this later must be able to tell a reviewed change
            # from one its own author waved through.
            "self_approved": plan.author_id == user.pk,
        },
    )
    return plan


@login_required
@require_http_methods(["GET", "POST"])
def plan_detail(request, pk, plan_id):
    from .code_factory import confirm_repository, prepare, publish, write_credential

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
            # Kept working for anything pointing here, but it now stops at the
            # summary: the pull request is its own decision on the run page.
            prepare(request.user, pk, run.pk if run else None)
            url = publish(request.user, pk, run.pk if run else None)
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
                chosen=request.POST.getlist("item"),
                declared=bool(request.POST.get("items_declared")),
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
            and (plan.author_id != request.user.pk or self_approval_allowed())
            and plan.status == "pending",
            # Why not, in the words of the rule that says not. A review form
            # that is simply absent reads as a missing feature.
            "no_review_because": (
                ""
                if grant.can_approve
                and (plan.author_id != request.user.pk or self_approval_allowed())
                and plan.status == "pending"
                else f"This plan was already {plan.get_status_display().lower()}."
                if plan.status != "pending"
                else "You wrote this plan, so somebody else has to review it. "
                "Approving your own work would make the gate a formality."
                if plan.author_id == request.user.pk
                else "You do not hold approval rights on this application."
            ),
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
