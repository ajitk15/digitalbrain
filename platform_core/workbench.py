"""Application knowledge, evidence retrieval and immutable approval workflows."""

import hashlib
import re
import threading
import uuid
from datetime import timedelta

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.http import (
    Http404,
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


def record_exchange(
    app, user, conversation, question, answer, citations, mode, status="complete", submission=""
):
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
        submission=submission,
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


def available_modes(graph_ai_enabled, ai_enabled=True):
    """Modes a conversation may be switched to. Search never needs configuration.

    Uses the same availability `mode_options` shows: a mode the selector marks
    "setup required" is not one a conversation can be put in.
    """
    return [
        (value, label)
        for value, label in CHAT_MODES
        if (value != "graph" or graph_ai_enabled) and (value != "ai" or ai_enabled)
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


def requested_mode(request, graph_ai_enabled, ai_enabled=True):
    """Mode for a conversation that does not exist yet.

    AI answers when they are set up, source search when they are not: the page
    once opened on "AI · setup required" - a disabled option, selected, with
    Send enabled - when a working mode was right there. A mode the reader asked
    for by name is kept only if it can actually answer.
    """
    source = request.POST if request.method == "POST" else request.GET
    usable = dict(available_modes(graph_ai_enabled, ai_enabled))
    mode = source.get("mode", "")
    if mode in usable:
        return mode
    if request.method == "POST":
        # A send is never quietly answered some other way: a mode asked for by
        # name and not available, or none at all, is refused with the reason.
        # Only the page picks a working default - and shows it, selected.
        return mode if mode in dict(CHAT_MODES) else "ai"
    return "ai" if "ai" in usable else "search"


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


def answer_question(
    user, app, conversation, question, mode, graph_version=None, trim_from=None, submission=""
):
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
        _, reply = record_exchange(
            app, user, conversation, question, answer, citations, mode, submission=submission
        )
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
    if request.method == "POST":
        form = QuestionForm(request.POST)
    else:
        # A starter question fills the composer and sends nothing: the reader
        # still presses Send, and can change it first. Works without script.
        form = QuestionForm(initial={"question": request.GET.get("q", "").strip()[:500]})
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
    # The revision this conversation actually answers from: its pinned version
    # when it has one, else the latest published. The banner and the answer
    # settings once disagreed - settings said v1 while the banner, measured
    # against the latest, said "answering from version 2".
    answering = published
    if conversation is not None and conversation.graph_version:
        from .models import GraphRevision

        answering = (
            GraphRevision.objects.filter(
                application=app, number=conversation.graph_version, published_at__isnull=False
            ).first()
            or published
        )
    answering_graph = answering.number if answering else None
    # How far that revision has moved from its sources. Shown, never
    # enforced: reproducibility is why answers come from a snapshot.
    published_changed, published_total = revision_drift(answering, app.pk) if answering else (0, 0)
    # Mode is a property of the conversation. A new conversation may be started in a
    # chosen mode; sending a message can never change it, so a paid mode is never
    # entered by accident.
    mode = (
        conversation.mode if conversation else requested_mode(request, graph_ai_enabled, ai_enabled)
    )
    if request.method == "POST" and form.is_valid():
        if mode not in dict(available_modes(graph_ai_enabled, ai_enabled)):
            form.add_error(None, unavailable_reason(mode))
        else:
            question = form.cleaned_data["question"]
            submission = submission_key(request)
            claim, first = claim_submission(app, request.user, submission)
            if not first:
                # The same send, again: a browser that lost the first response
                # cannot tell whether it was saved. Show what was saved - or, if
                # the first request is still being handled, say so. Never ask,
                # and pay for, the same question twice.
                destination = reverse("chat", args=[pk]) + (
                    f"?conversation={claim.conversation_id}" if claim.conversation_id else ""
                )
                if not claim.conversation_id:
                    messages.info(
                        request,
                        "That question is still being answered. It appears in your "
                        "conversations when it is done - there is no need to send it again.",
                    )
                if wants_stream(request):
                    return JsonResponse({"duplicate": True, "redirect": destination}, status=409)
                return redirect(destination)
            if mode in {"ai", "graph"} and wants_stream(request):
                # Streaming path: persist the exchange now and let the browser
                # open the event stream. The synchronous branch below stays the
                # no-JavaScript fallback and is never removed.
                try:
                    conversation, placeholder = start_answer(
                        app,
                        request.user,
                        conversation,
                        question,
                        mode,
                        graph_version=new_graph_version(request, app.pk, graph_ai_enabled),
                        submission=submission,
                    )
                except Exception:
                    release_submission(claim)
                    raise
                settle_submission(claim, conversation)
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
                    submission=submission,
                )
                settle_submission(claim, conversation)
                return redirect(f"{reverse('chat', args=[pk])}?conversation={conversation.pk}")
            except (ValidationError, ImproperlyConfigured) as error:
                # Nothing was saved, so the key is free again. The page that
                # re-renders carries a new one anyway.
                release_submission(claim)
                form.add_error(None, failure_text(error))
            except Exception:
                # Anything else: the key goes back if nothing was saved, so
                # the same retry is answered rather than refused as a repeat.
                release_submission(claim)
                raise
    history_messages = (
        conversation.messages.filter(application=app, user=request.user).order_by(
            "sequence", "created_at", "id"
        )
        if conversation
        else ChatMessage.objects.none()
    )
    message_pages = Paginator(history_messages, 60)
    message_page = message_pages.get_page(request.GET.get("messages", message_pages.num_pages))
    message_page.object_list = list(message_page.object_list)
    for message in message_page.object_list:
        # An answer still marked streaming that no stream in this process is
        # producing: its request was lost, or the server restarted mid-answer.
        # chat.js resumes it; without script, Regenerate is offered.
        message.stranded = (
            message.status == "streaming" and streaming.get_session(message.pk) is None
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
            # A fresh key per rendered composer; chat.js makes a new one after
            # each send it completes without reloading the page.
            "submission": uuid.uuid4(),
            "starters": chat_starters(app, mode) if conversation is None else [],
            # What the collapsed "Answer settings" line says is in effect.
            "mode_label": dict(CHAT_MODE_LABELS).get(mode, mode),
            "answer_version": answering_graph,
            # A newer published version than the one this conversation is
            # pinned to, named separately rather than as what answers.
            "newer_published": published_graph
            if answering_graph and published_graph and published_graph != answering_graph
            else None,
        },
    )


#: How each answer mode reads on the settings line.
CHAT_MODE_LABELS = (("search", "Source excerpts"), ("ai", "AI answer"), ("graph", "Graph answer"))


def chat_starters(app, mode="ai"):
    """Three questions worth asking any application first, for an empty chat.

    Suggestions only: each fills the composer and sends nothing. Shaped by what
    the application is for, so an operations team is not offered code questions,
    and by how answers are made here: source search returns matching passages
    and cannot summarise, so it is offered things to find rather than questions
    that promise a synthesis it will not give.
    """
    from .services import purposes

    serving = purposes(app)
    if mode == "search":
        starters = [f"What {app.name} does and who uses it"]
        if "operations" in serving:
            starters += ["Deployment and rollback steps", "Past incidents and their causes"]
        if "engineering" in serving:
            starters += ["Requirements not met yet", "Where the system design is written down"]
        if len(starters) == 1:
            starters += ["How it is deployed and operated", "Known risks"]
        return starters[:3]
    starters = [f"Summarise what {app.name} does"]
    if "operations" in serving:
        starters.append("How is a release rolled back?")
        starters.append("What has caused incidents before?")
    if "engineering" in serving:
        starters.append("Which requirements are not met yet?")
        starters.append("How is the system structured?")
    if len(starters) == 1:
        starters.append("How is it deployed and operated?")
        starters.append("What are the open risks?")
    return starters[:3]


def submission_key(request):
    """The composer's one-time key, if it is one. Anything else is no key."""
    try:
        return str(uuid.UUID(request.POST.get("submission", "")))
    except ValueError:
        return ""


#: How old a claim with nothing saved under it must be before it is taken for
#: abandoned - its request died, most often in a restart. Longer than any
#: answer may run (the runtimes stop a provider call at 120 seconds) with room
#: to spare, so a request that is merely slow is never answered twice.
CLAIM_ABANDONED_AFTER = timedelta(minutes=5)


def saved_under(app, user, key):
    """The conversation a send with this key saved its exchange in, if it did."""
    return (
        ChatMessage.objects.filter(application=app, user=user, submission=key)
        .values_list("conversation_id", flat=True)
        .first()
    )


def claim_submission(app, user, key):
    """(claim, True) for the request that may answer this key; (claim, False) after.

    The claim is written before anything else, in a transaction of its own, and
    the unique constraint decides the race: of two overlapping requests exactly
    one inserts, and the other is told it is a repeat. No key, no claim.

    A repeat is not always a duplicate of something that exists. If the send
    saved its exchange, the claim is pointed at it, so the repeat is shown it.
    If nothing was saved and the claim is older than any answer can take, its
    request died: the claim is taken over - by exactly one retry, since only one
    guarded delete can succeed - and this send is answered rather than refused
    forever. Otherwise the first request is still working.
    """
    from django.db import IntegrityError

    from .models import ChatSubmission

    if not key:
        return None, True
    for _ in range(2):
        try:
            with transaction.atomic():
                return ChatSubmission.objects.create(application=app, user=user, key=key), True
        except IntegrityError:
            pass
        claim = ChatSubmission.objects.filter(application=app, user=user, key=key).first()
        if claim is None:
            continue  # released between the insert and the read; try again
        saved = saved_under(app, user, key)
        if saved:
            if claim.conversation_id != saved:
                ChatSubmission.objects.filter(pk=claim.pk).update(conversation_id=saved)
                claim.conversation_id = saved
            return claim, False
        cutoff = timezone.now() - CLAIM_ABANDONED_AFTER
        abandoned, _ = ChatSubmission.objects.filter(pk=claim.pk, created_at__lt=cutoff).delete()
        if not abandoned:
            return claim, False
    return ChatSubmission.objects.get(application=app, user=user, key=key), False


def release_submission(claim):
    """Give a key back after a send that failed - but only if it saved nothing.

    Freeing it after the exchange was written would let the same send be asked,
    and paid for, a second time.
    """
    from .models import ChatSubmission

    if claim is None or saved_under(claim.application, claim.user, claim.key):
        return
    ChatSubmission.objects.filter(pk=claim.pk).delete()


def settle_submission(claim, conversation):
    """Point a claim at the conversation its send landed in."""
    from .models import ChatSubmission

    if claim is not None:
        ChatSubmission.objects.filter(pk=claim.pk).update(conversation=conversation)


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


def start_answer(app, user, conversation, question, mode, graph_version=None, submission=""):
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
            submission=submission,
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

    conversation = ChatConversation.objects.filter(messages__id=message_id).distinct().first()
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
    return [{"id": c["id"], "title": c["title"], "excerpt": c["excerpt"]} for c in citations or []]


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
    return get_object_or_404(ChatConversation, pk=conversation_id, application=app, user=user)


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
                return redirect(f"{reverse('chat', args=[pk])}?conversation={conversation.pk}")
            conversation.graph_version = chosen
        conversation.save(update_fields=["graph_version", "updated_at"])
    elif action == "mode":
        graph_ai_enabled = AIConfiguration.objects.filter(
            application=app, purpose="graph_retrieval", enabled=True
        ).exists()
        ai_enabled = AIConfiguration.objects.filter(
            application=app, purpose="chat", enabled=True
        ).exists()
        mode = request.POST.get("mode", "")
        if mode not in dict(available_modes(graph_ai_enabled, ai_enabled)):
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
    if answer.status == "streaming" and streaming.get_session(answer.pk) is not None:
        # Still being written by a live stream: replacing it now would race the
        # worker that owns the row.
        messages.info(request, "That answer is still being written. Wait for it to finish.")
        return redirect(f"{reverse('chat', args=[pk])}?conversation={conversation.pk}")
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
                    "steps": [(order[step.key], step) for step in found if step.gate == key],
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
