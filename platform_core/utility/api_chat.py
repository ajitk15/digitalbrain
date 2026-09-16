"""A chat endpoint for machine callers, JSON or server-sent events.

This is the one `/api/v1/` surface that calls a model, and therefore the one
that spends the application's provider budget. `graph/search/` and the MCP
server deliberately do not, so the guard here is a feature switch an owner has
to turn on: `chat_api` is written disabled for every application that already
existed, and starts unticked on the create form.

Everything else is the browser's, unchanged. The token names a user, `access`
runs the same checks a session request runs, and answering goes through
`workbench.answer_question` (JSON) or `workbench.start_answer` plus the same
streaming worker the browser uses (SSE). There is no second answering path, no
second permission model, and no way to reach another user's conversation: the
conversation is looked up by the token's own user.

The model and the credential are the application's own. A caller chooses
nothing about spend beyond asking a question.
"""

import json
import threading
import uuid

from django.core.exceptions import PermissionDenied, ValidationError
from django.http import JsonResponse, StreamingHttpResponse
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from ..agent_runtime import streaming
from ..api import authorize, error
from ..api_auth import ApiError
from ..models import ChatConversation, ChatMessage
from ..services import audit, feature_enabled

#: Long enough for a real question, short enough that an essay cannot be posted
#: as one. The browser composer is bounded too.
MAX_QUESTION = 4000

MODES = {"search", "ai", "graph"}


def authorize_chat(request, reference):
    """Token, platform access, then the switch that allows billed API answers."""
    user, token, app = authorize(request, reference)
    if not feature_enabled("chat_api", app):
        raise ApiError(
            "The Chat API is switched off for this application. An owner can "
            "enable it under Settings > Features.",
            status=403,
            code="chat_api_disabled",
        )
    return user, token, app


def payload(request):
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        raise ApiError("Send a JSON object body.", code="invalid_request") from None
    if not isinstance(body, dict):
        raise ApiError("Send a JSON object body.", code="invalid_request")
    return body


def question_of(body):
    value = body.get("question")
    if value is not None and not isinstance(value, str):
        raise ApiError("question must be a string.", code="invalid_request")
    value = (value or "").strip()
    if not value:
        raise ApiError("Provide a question.", code="invalid_request")
    return value[:MAX_QUESTION]


def conversation_of(body, app, user):
    """An existing conversation of this user's, or None to start a new one.

    Scoped to the token's user as well as to the application: a token acts as
    the person who issued it, so it must not read or extend somebody else's
    transcript. A conversation that belongs to another user reads as "not
    found", never as "forbidden".
    """
    value = body.get("conversation_id")
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ApiError("conversation_id must be a string.", code="invalid_request")
    try:
        identity = uuid.UUID(value)
    except ValueError:
        raise ApiError("No such conversation.", status=404, code="not_found") from None
    conversation = ChatConversation.objects.filter(
        pk=identity, application=app, user=user
    ).first()
    if conversation is None:
        raise ApiError("No such conversation.", status=404, code="not_found")
    return conversation


def mode_of(body, conversation):
    """The answer mode, which belongs to the conversation once one exists.

    A follow-up cannot move a thread onto a different mode, exactly as the
    browser cannot: sending a message never changes what produced the answers
    before it, and never moves a thread onto a paid mode by accident.
    """
    if conversation is not None:
        return conversation.mode
    value = body.get("mode", "ai")
    if value not in MODES:
        raise ApiError("mode must be one of: search, ai, graph.", code="invalid_request")
    return value


def rendered(message):
    """One answer, with the citations that survived verification."""
    return {
        "message_id": str(message.pk),
        "answer": message.body,
        "mode": message.mode,
        "citations": [
            {
                "source_id": citation.get("id"),
                "source_title": citation.get("title"),
                # The digest stays server-side, as it does on graph/search/: it
                # is an integrity token, not something a caller needs.
                "evidence": citation.get("excerpt"),
            }
            for citation in (message.citations or [])
        ],
    }


@csrf_exempt
@require_http_methods(["POST"])
def chat(request, reference):
    """Ask one question. Returns the answer, or a handle to stream it."""
    from ..workbench import answer_question, start_answer

    try:
        user, _, app = authorize_chat(request, reference)
        body = payload(request)
        question = question_of(body)
        conversation = conversation_of(body, app, user)
        mode = mode_of(body, conversation)
        # Only ai and graph stream, exactly as the browser decides it: the
        # worker has no lexical branch and would call a model for a mode whose
        # whole point is that it does not. Search answers synchronously instead
        # of erroring - there is simply nothing to stream.
        if body.get("stream") and mode in {"ai", "graph"}:
            conversation, message = start_answer(app, user, conversation, question, mode)
            started = JsonResponse(
                {
                    "conversation_id": str(conversation.pk),
                    "message_id": str(message.pk),
                    "stream_url": reverse("api-chat-stream", args=[reference, message.pk]),
                    "mode": mode,
                },
                status=202,
            )
            audit(
                user,
                "chat.api_question",
                app.pk,
                app.product.portfolio.organization,
                details={"mode": mode, "stream": True},
            )
            return started
        try:
            # Returns the conversation; the reply is its newest assistant row,
            # ordered by `sequence` because both halves are written in one
            # transaction and timestamps cannot separate them.
            conversation = answer_question(user, app, conversation, question, mode)
            message = (
                conversation.messages.filter(role="assistant").order_by("-sequence").first()
            )
        except ValidationError as failure:
            raise ApiError(
                " ".join(failure.messages), status=409, code="answer_unavailable"
            ) from None
        except PermissionDenied:
            raise ApiError(
                "This token's user no longer has access.", status=403, code="forbidden"
            ) from None
    except ApiError as failure:
        return error(failure)
    audit(
        user,
        "chat.api_question",
        app.pk,
        app.product.portfolio.organization,
        details={"mode": mode, "stream": False},
    )
    return JsonResponse({"conversation_id": str(conversation.pk), **rendered(message)})


@csrf_exempt
@require_http_methods(["GET"])
def chat_stream(request, reference, message_id):
    """Server-sent events for one answer started with `"stream": true`.

    Authorization runs before the response is constructed: raising inside the
    generator would arrive after the 200 headers and truncate the body rather
    than returning 403. Same reason the browser's stream view does it here.
    """
    from ..workbench import answer_worker, conversation_context

    try:
        user, _, app = authorize_chat(request, reference)
    except ApiError as failure:
        return error(failure)
    missing = ApiError("No answer is streaming here.", status=404, code="not_found")
    message = ChatMessage.objects.filter(
        pk=message_id,
        application=app,
        user=user,
        role="assistant",
        status="streaming",
    ).first()
    if message is None:
        return error(missing)
    conversation = message.conversation
    history = conversation_context(conversation, app, user)
    question = (
        conversation.messages.filter(role="user", sequence=message.sequence - 1)
        .values_list("body", flat=True)
        .first()
    )
    if not question:
        return error(missing)
    try:
        session = streaming.open_session(message.pk)
    except streaming.StreamCapacityError as full:
        return error(ApiError(str(full), status=503, code="busy"))
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
        name=f"api-chat-answer-{message.pk}",
        daemon=True,
    ).start()
    response = StreamingHttpResponse(
        streaming.stream_frames(session), content_type="text/event-stream"
    )
    # No Content-Length, or waitress buffers the whole body instead of chunking.
    response["Cache-Control"] = "no-cache, no-transform"
    response["X-Accel-Buffering"] = "no"
    return response
