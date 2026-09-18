"""Bounded WSGI server. Keep the listener private behind a TLS proxy in production."""

import argparse
import sys
from pathlib import Path
from threading import Thread

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from waitress import serve  # noqa: E402

from digitalbrain.wsgi import application  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, default=8000)
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--instance", required=True, help="Non-secret process ownership marker.")
parser.add_argument(
    "--trusted-proxy",
    default="",
    help=(
        "Address of the single reverse proxy allowed to set X-Forwarded-*. "
        "Required behind a TLS terminator; omit when nothing fronts this."
    ),
)
args = parser.parse_args()

#: Waitress discards X-Forwarded-* from anyone it was not told to trust, which
#: is the right default and the reason this flag has to exist. In production
#: SECURE_SSL_REDIRECT is on and SECURE_PROXY_SSL_HEADER reads
#: X-Forwarded-Proto: with the header discarded, Django sees a plain-HTTP
#: request arriving over the proxy's HTTPS connection, redirects to HTTPS, and
#: the proxy forwards the same request again - an infinite redirect that looks
#: like a misconfigured certificate and is not one.
#:
#: Waitress compares the peer address to this value as a string, so an exact
#: address is the tight setting and the one to prefer. Behind a proxy whose
#: container address is assigned at start - Coolify, Compose, anything on a
#: bridge network - there is no stable address to name, and a hostname can
#: never match because the peer is an IP. "*" is the answer there, and it is
#: only sound under one condition: NOTHING MAY REACH THIS LISTENER EXCEPT THE
#: PROXY. Publish no ports from the container and keep it on the proxy's
#: private network. Reachable directly with "*" set, any client can declare its
#: own scheme and its own address - which forges the IP that lockout counts
#: failures against, and tells Django a plain request arrived over TLS.
proxy = {}
if args.trusted_proxy:
    proxy = {
        "trusted_proxy": args.trusted_proxy,
        "trusted_proxy_count": 1,
        "trusted_proxy_headers": {"x-forwarded-for", "x-forwarded-proto", "x-forwarded-host"},
        # Anything the proxy did not set is removed rather than passed through,
        # so a header a client invented cannot survive one honest hop.
        "clear_untrusted_proxy_headers": True,
    }
from platform_core.agent_runtime.streaming import MAX_ACTIVE_STREAMS  # noqa: E402
from platform_core.document_worker import run_document_worker  # noqa: E402

#: A streaming answer holds its request thread until it finishes, so the pool has
#: to outnumber the streams that can exist - otherwise a full complement of
#: answers leaves nothing to serve the Stop button that would end them.
REQUEST_THREADS = MAX_ACTIVE_STREAMS * 2

Thread(target=run_document_worker, name="document-conversion", daemon=True).start()
serve(
    application,
    host=args.host,
    port=args.port,
    threads=REQUEST_THREADS,
    connection_limit=100,
    channel_timeout=60,
    cleanup_interval=10,
    max_request_header_size=32768,
    max_request_body_size=23068672,
    expose_tracebacks=False,
    ident="DigitalBrain",
    **proxy,
)
