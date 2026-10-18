"""The ASGI entry point: streams on the event loop, every other request as WSGI.

Used when the configuration says `web_server = "uvicorn"` (`scripts/serve.py`).
Under waitress each streamed answer held a request thread for its whole life,
so a process served `MAX_ACTIVE_STREAMS` readers at most. Here the two stream
routes - the browser's `chat-stream` and the API's `api-chat-stream` - go to
Django's ASGI handler, whose reader awaits (`streaming.attach(asynchronous=
True)`), and every other request goes to the same WSGI application as before,
run in a bounded thread pool by `a2wsgi`. Views, middleware and the CSP are
unchanged; only what waits for an answer's next words changed.

Waitress did three things at the edge that uvicorn does not, so `Router` does
them: it trusts X-Forwarded-* only from the one proxy named and removes them
from anyone else (`--trusted-proxy`, as waitress's `clear_untrusted_proxy_headers`),
and it refuses a body over `MAX_BODY` or of no stated length
(`max_request_body_size`).
"""

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "digitalbrain.settings")

from django.core.asgi import get_asgi_application  # noqa: E402

#: Waitress's `max_request_body_size`: a little over Django's own upload limit.
MAX_BODY = 23068672
#: The routes whose responses wait on an answer.
STREAM_ROUTES = frozenset({"chat-stream", "api-chat-stream"})
#: Every forwarding header; only the first three are believed, and only from
#: the trusted proxy.
TRUSTED_HEADERS = (b"x-forwarded-for", b"x-forwarded-proto", b"x-forwarded-host")
FORWARDING = TRUSTED_HEADERS + (b"x-forwarded-port", b"x-forwarded-by", b"forwarded")


def is_stream(path):
    from django.urls import Resolver404, resolve

    try:
        return resolve(path).url_name in STREAM_ROUTES
    except Resolver404:
        return False


def last_hop(value):
    """The address the one trusted proxy saw, as waitress takes it (count 1)."""
    return value.decode("latin-1").split(",")[-1].strip()


class Router:
    def __init__(self, streams, pages, trusted_proxy="", max_body=MAX_BODY):
        self.streams, self.pages = streams, pages
        self.trusted, self.max_body = trusted_proxy, max_body

    def forwarded(self, scope):
        peer = (scope.get("client") or ("", 0))[0]
        trusted = bool(self.trusted) and self.trusted in {"*", peer}
        if not trusted:
            headers = [(k, v) for k, v in scope["headers"] if k not in FORWARDING]
            return {**scope, "headers": headers}
        found = {k: v for k, v in scope["headers"] if k in TRUSTED_HEADERS}
        headers = [(k, v) for k, v in scope["headers"] if k not in FORWARDING or k in found]
        scope = {**scope}
        if b"x-forwarded-for" in found:
            scope["client"] = (last_hop(found[b"x-forwarded-for"]), 0)
        proto = last_hop(found.get(b"x-forwarded-proto", b"")).lower()
        if proto in {"http", "https"}:
            scope["scheme"] = proto
        if b"x-forwarded-host" in found:
            host = last_hop(found[b"x-forwarded-host"]).encode("latin-1")
            headers = [(k, v) for k, v in headers if k != b"host"] + [(b"host", host)]
        scope["headers"] = headers
        return scope

    async def refuse(self, send, status, text):
        body = text.encode()
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                        (b"content-length", str(len(body)).encode())],
        })
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            # No websockets here; refuse rather than leave the socket open.
            await send({"type": "websocket.close", "code": 1008})
            return
        scope = self.forwarded(scope)
        headers = dict(scope["headers"])
        length = headers.get(b"content-length")
        if length is not None:
            if not length.isdigit() or int(length) > self.max_body:
                await self.refuse(send, 413, "Request body too large.")
                return
        elif b"transfer-encoding" in headers:
            await self.refuse(send, 411, "A request body needs a Content-Length.")
            return
        app = self.streams if is_stream(scope["path"]) else self.pages
        await app(scope, receive, send)


def build(trusted_proxy="", workers=24):
    from a2wsgi import WSGIMiddleware

    from digitalbrain.wsgi import application as wsgi_application

    return Router(
        get_asgi_application(),
        WSGIMiddleware(wsgi_application, workers=workers),
        trusted_proxy=trusted_proxy,
    )


application = build()
