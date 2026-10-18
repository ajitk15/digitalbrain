"""Chat: the conversation screens, their submissions, and the streamed answer.

`chat` is a real POST form that answers synchronously through
`workbench.answer_question` with JavaScript off; `chat_stream` is the
EventSource the browser opens otherwise. The worker thread (`answer_worker`,
`run_answer`) owns the provider call and every database write for its
message, and the SSE generator does no ORM work (CLAUDE.md, Chat shape).
Split out of `workbench`, which keeps conversation storage and answering.
"""

import threading
import uuid
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.http import Http404, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from . import metrics
from .agent_runtime import streaming
from .models import AIConfiguration, ChatConversation, ChatMessage
from .services import audit
from .workbench import (
    QuestionForm,
    access,
    answer_question,
    available_modes,
    clear_conversations,
    conversation_context,
    credential_available,
    failure_text,
    graph_version_choices,
    lexical_citations,
    lock_conversation,
    mode_options,
    new_graph_version,
    next_sequence,
    requested_mode,
    retention_days,
    retitle,
    retrieval_query,
    unavailable_reason,
    wants_stream,
)


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
    from .graphs import published_revision, revision_added, revision_drift

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
            )
            .defer("data")
            .first()
            or published
        )
    answering_graph = answering.number if answering else None
    # How far that revision has moved from its sources. Shown, never
    # enforced: reproducibility is why answers come from a snapshot.
    published_changed, published_total = revision_drift(answering, app.pk) if answering else (0, 0)
    published_added = revision_added(answering, app.pk) if answering else 0
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
                        # Whether a dropped connection may reconnect: with Redis
                        # the answer carries on and is replayed from where the
                        # browser left off; without it, reconnecting would start
                        # the answer again.
                        "resumes": streaming.resumable(),
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
        live = message.status == "streaming" and streaming.is_live(message.pk)
        message.stranded = message.status == "streaming" and not live
        # With Redis an answer another tab or process is writing can be
        # followed from its first word, so the page picks it up too.
        message.following = live and streaming.resumable()
        message.resumes = streaming.resumable()
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
            "published_added": published_added,
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
    finished = timezone.now()
    closed = ChatMessage.objects.filter(pk=message_id, status="streaming").update(
        status=status,
        body=body,
        citations=citations,
        error=error[:300],
        provider=provider,
        model=model,
        finished_at=finished,
    )
    if closed and metrics.enabled():
        asked = ChatMessage.objects.filter(pk=message_id).values_list("created_at", "mode")
        for created, mode in asked:
            metrics.record("digitalbrain.answer.duration",
                           (finished - created).total_seconds(),
                           mode=mode, provider=provider or "none", outcome=status)
    return closed


def answer_worker(
    user_id, app_id, message_id, question, history, session, mode="ai", graph_version=None
):
    """`run_answer`, on a connection narrowed to its application's rows (tenancy.py).

    A thread of its own has a connection of its own, which no request binds.
    """
    from . import tenancy

    with tenancy.scoped(app_id):
        run_answer(user_id, app_id, message_id, question, history, session, mode, graph_version)


def run_answer(user_id, app_id, message_id, question, history, session, mode, graph_version):
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

            citations_seed = graph_citations(app.pk, query, version=graph_version, user=user)
        else:
            citations_seed = lexical_citations(app, query, user)
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
        ChatMessage, pk=message_id, application=app, user=request.user, role="assistant"
    )
    # A finished answer is still replayed while Redis holds its events: a
    # browser reconnecting after the last word was written must get the words
    # it missed, not a 404. It is never started again - see `stream_answer`.
    if message.status != "streaming" and not streaming.replayable(message.pk):
        raise Http404
    history = conversation_context(message.conversation, app, request.user)
    question = (
        message.conversation.messages.filter(role="user", sequence=message.sequence - 1)
        .values_list("body", flat=True)
        .first()
    )
    if not question:
        raise Http404
    try:
        frames = stream_answer(request, request.user, app, message, question, history)
    except streaming.StreamCapacityError as full:
        return JsonResponse({"error": str(full)}, status=503)
    return event_stream(frames)


def stream_answer(request, user, app, message, question, history, name="chat-answer"):
    """The frames for one answer: its worker started here unless it already runs.

    One definition for the browser's stream and the API's. With Redis a request
    may be a reconnect - it carries `Last-Event-ID` - and then follows the
    answer instead of starting it, on whichever process it reached.
    """
    conversation = message.conversation
    # Only an answer still marked streaming may be started or closed out; a
    # finished one is only ever replayed.
    live = message.status == "streaming"

    def start(session):
        threading.Thread(
            target=answer_worker,
            args=(
                user.pk,
                app.pk,
                message.pk,
                question,
                history,
                session,
                conversation.mode,
                conversation.graph_version,
            ),
            name=f"{name}-{message.pk}",
            daemon=True,
        ).start()

    def interrupted(text):
        finish_answer(message.pk, "failed", text, [], streaming.INTERRUPTED)

    return streaming.attach(
        message.pk,
        request.headers.get("Last-Event-ID"),
        start if live else None,
        interrupted if live else None,
        asynchronous=served_by_event_loop(request),
    )


def served_by_event_loop(request):
    """Whether this request arrived through ASGI, whose response must not block.

    Under `web_server = "uvicorn"` the stream endpoints are routed to Django's
    ASGI handler (`digitalbrain/asgi.py`): this view still runs in a thread,
    but the frames it returns are read by the event loop, so they must be an
    async iterator that awaits rather than one that holds a thread.
    """
    from django.core.handlers.asgi import ASGIRequest

    return isinstance(request, ASGIRequest)


def event_stream(frames):
    response = StreamingHttpResponse(frames, content_type="text/event-stream")
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
    if answer.status == "streaming" and streaming.is_live(answer.pk):
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
