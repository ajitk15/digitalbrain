"""Machine-facing graph retrieval: a REST endpoint and an MCP server.

Both surfaces answer the same question - "what does the published graph say about
this?" - and both return the same verified evidence chat would use. Neither calls
a model, so a caller cannot spend the application's provider budget through them.

Authorization is the browser's, not a parallel one: the bearer token names a user
and an application, and `access` then runs the same checks a session request runs.
A revoked grant or a disabled feature switch closes these endpoints immediately.

Versions are first class. A request may pin `version`; without one the latest
*published* revision answers, never a draft, and every response states which
version produced it so a caller can pin it deliberately afterwards.
"""

import json

from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .api_auth import ApiError, authenticate
from .services import audit
from .workbench import access

MAX_QUESTION = 500
PROTOCOL_VERSION = "2025-06-18"

TOOLS = [
    {
        "name": "search_knowledge_graph",
        "description": (
            "Search an application's published knowledge graph. Returns relationships "
            "that match the question, each with the source document and the exact quoted "
            "evidence supporting it. Every result is verified against the live source at "
            "request time, so a relationship whose evidence has changed is not returned."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "What to look for, in natural language.",
                },
                "version": {
                    "type": "integer",
                    "description": (
                        "Graph version to answer from. Omit for the latest published "
                        "version. Pin this for reproducible results."
                    ),
                },
            },
            "required": ["question"],
        },
    }
]


def error(exc):
    return JsonResponse({"error": {"code": exc.code, "message": exc.message}}, status=exc.status)


def search(user, app_id, question, version):
    """Verified graph evidence for a question, plus the version that answered."""
    from .graph_ai import graph_citations, graph_snapshot

    question = (question or "").strip()[:MAX_QUESTION]
    if not question:
        raise ApiError("Provide a question.", code="invalid_request")
    if version is not None:
        try:
            version = int(version)
        except (TypeError, ValueError):
            raise ApiError("version must be an integer.", code="invalid_request") from None
    try:
        _, number = graph_snapshot(app_id, version)
        citations = graph_citations(app_id, question, version=version)
    except ValidationError as failure:
        raise ApiError(
            " ".join(failure.messages), status=409, code="graph_unavailable"
        ) from None
    return {
        "question": question,
        "version": number,
        "results": [
            {
                "source_id": citation["id"],
                "source_title": citation["title"],
                # The digest stays server-side: it is an integrity token used to
                # verify a citation, not something a caller needs.
                "evidence": citation["excerpt"],
            }
            for citation in citations
        ],
        "count": len(citations),
    }


def authorize(request, app_id):
    """Authenticate the token, then apply the platform's own access rules."""
    user, token = authenticate(request, app_id)
    try:
        app, _ = access(user, app_id, "knowledge")
        access(user, app_id, "chat")
    except PermissionDenied:
        raise ApiError(
            "This token's user no longer has access to that application or feature.",
            status=403,
            code="forbidden",
        ) from None
    except Http404:
        raise ApiError("No such application.", status=404, code="not_found") from None
    return user, token, app


@csrf_exempt
@require_http_methods(["GET", "POST"])
def graph_search(request, pk):
    """GET or POST a question, receive verified graph evidence."""
    try:
        user, token, app = authorize(request, pk)
        if request.method == "POST":
            try:
                body = json.loads(request.body or b"{}")
            except ValueError:
                raise ApiError("Send a JSON object.", code="invalid_request") from None
            if not isinstance(body, dict):
                raise ApiError("Send a JSON object.", code="invalid_request")
            question, version = body.get("question"), body.get("version")
        else:
            question = request.GET.get("q") or request.GET.get("question")
            version = request.GET.get("version") or None
        payload = search(user, pk, question, version)
    except ApiError as failure:
        return error(failure)
    audit(
        user,
        "api.graph_search",
        app.pk,
        app.product.portfolio.organization,
        details={"token": token.name, "version": payload["version"], "results": payload["count"]},
    )
    return JsonResponse(payload)


def rpc_result(request_id, result):
    return JsonResponse({"jsonrpc": "2.0", "id": request_id, "result": result})


def rpc_error(request_id, code, message, status=200):
    return JsonResponse(
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}},
        status=status,
    )


@csrf_exempt
@require_http_methods(["POST"])
def mcp(request, pk):
    """A read-only MCP server exposing graph retrieval as one tool.

    Implements the three methods a tool server needs - initialize, tools/list and
    tools/call - as JSON-RPC over HTTP. Errors a caller can act on are returned as
    JSON-RPC errors with HTTP 200, per the protocol; only authentication and
    transport problems use an HTTP status.
    """
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        return rpc_error(None, -32700, "Parse error", status=400)
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
        return rpc_error(None, -32600, "Invalid Request", status=400)

    request_id = body.get("id")
    method = body.get("method")
    params = body.get("params") if isinstance(body.get("params"), dict) else {}

    try:
        user, token, app = authorize(request, pk)
    except ApiError as failure:
        return error(failure)

    if method == "initialize":
        return rpc_result(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": f"digital-brain-graph:{app.name}", "version": "1.0.0"},
            },
        )

    if method in {"notifications/initialized", "ping"}:
        return rpc_result(request_id, {})

    if method == "tools/list":
        return rpc_result(request_id, {"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name")
        if name != TOOLS[0]["name"]:
            return rpc_error(request_id, -32602, f"Unknown tool: {name}")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        try:
            payload = search(user, pk, arguments.get("question"), arguments.get("version"))
        except ApiError as failure:
            # A tool failure is a result the model can read and react to, not a
            # protocol error.
            return rpc_result(
                request_id,
                {"content": [{"type": "text", "text": failure.message}], "isError": True},
            )
        audit(
            user,
            "api.graph_search",
            app.pk,
            app.product.portfolio.organization,
            details={
                "token": token.name,
                "transport": "mcp",
                "version": payload["version"],
                "results": payload["count"],
            },
        )
        return rpc_result(
            request_id,
            {
                "content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
                "structuredContent": payload,
                "isError": False,
            },
        )

    return rpc_error(request_id, -32601, f"Method not found: {method}")
