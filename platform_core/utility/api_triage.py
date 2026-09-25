"""Bearer-only ServiceOps triage API using the browser's access rules."""

import json
import uuid

from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from ..api_auth import ApiError, authenticate
from ..models import TriageRun
from ..serviceops import incident_queryset, verified
from ..serviceops_triage import brief_payload, create_run
from ..services import feature_enabled
from ..workbench import access


def _error(failure):
    return JsonResponse(
        {"error": {"code": failure.code, "message": failure.message}},
        status=failure.status,
    )


def _authorize(request, reference, *, write=False):
    user, _, app = authenticate(request, reference)
    try:
        access(user, app.pk, "service_ops", write=write)
        access(user, app.pk, "knowledge")
    except PermissionDenied:
        raise ApiError("ServiceOps access is disabled.", status=403, code="forbidden") from None
    except Http404:
        raise ApiError("No such application.", status=404, code="not_found") from None
    if write and not feature_enabled("chat_api", app):
        raise ApiError("The Chat API is disabled.", status=403, code="chat_api_disabled")
    return user, app


def _uuid(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise ApiError("No such incident or run.", status=404, code="not_found") from None


def _brief(run):
    try:
        return brief_payload(run)
    except ValidationError:
        raise ApiError(
            "The brief's evidence has changed; triage again.",
            status=409,
            code="stale_evidence",
        ) from None


@csrf_exempt
@require_POST
def triage(request, reference):
    try:
        user, app = _authorize(request, reference, write=True)
        try:
            body = json.loads(request.body or b"{}")
        except ValueError:
            raise ApiError("Send a JSON object.") from None
        if not isinstance(body, dict):
            raise ApiError("Send a JSON object.")
        incident = incident_queryset(app).filter(pk=_uuid(body.get("incident_id"))).first()
        if not incident or not verified(incident):
            raise ApiError("No such incident.", status=404, code="not_found")
        run = create_run(user, app.pk, incident, trigger="api")
        return JsonResponse(_brief(run), status=201)
    except ValidationError as failure:
        return _error(ApiError(" ".join(failure.messages), status=409, code="triage_unavailable"))
    except ApiError as failure:
        return _error(failure)


@csrf_exempt
@require_GET
def brief(request, reference, run_id):
    try:
        _, app = _authorize(request, reference)
        run = TriageRun.objects.filter(pk=_uuid(run_id), application=app).first()
        if not run:
            raise ApiError("No such run.", status=404, code="not_found")
        return JsonResponse(_brief(run))
    except ApiError as failure:
        return _error(failure)
