import logging
import time
import uuid

from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import redirect

from .observability import request_id_context

logger = logging.getLogger("digitalbrain.requests")


class RequestContextMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = str(uuid.uuid4())
        request.request_id = request_id
        token = request_id_context.set(request_id)
        started = time.perf_counter()
        try:
            try:
                length = int(request.META.get("CONTENT_LENGTH") or 0)
            except ValueError:
                length = -1
            if length < 0:
                response = HttpResponse("Invalid content length.", status=400)
            elif length > settings.DATA_UPLOAD_MAX_MEMORY_SIZE:
                response = HttpResponse("Request is too large.", status=413)
            else:
                response = self.get_response(request)
            response["X-Request-ID"] = request_id
            response["Content-Security-Policy"] = (
                "default-src 'none'; img-src 'self'; style-src 'self'; script-src 'self'; "
                "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
            )
            response["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
            logger.info(
                "request",
                extra={
                    "event": "http.request",
                    "request_id": request_id,
                    "route": getattr(getattr(request, "resolver_match", None), "url_name", None),
                    "status": response.status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
            return response
        finally:
            request_id_context.reset(token)


class ResponsePolicyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (
            request.user.is_authenticated
            and request.user.must_change_password
            and request.path
            not in {"/accounts/password/", "/accounts/logout/", "/branding/logo.png"}
            and not request.path.startswith("/static/")
        ):
            response = redirect("password")
        else:
            response = self.get_response(request)
        if request.user.is_authenticated or request.path.startswith("/accounts/"):
            response["Cache-Control"] = "no-store"
        return response
