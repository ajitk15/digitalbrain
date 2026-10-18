"""Bounded web server. Keep the listener private behind a TLS proxy in production.

Waitress by default. With `web_server = "uvicorn"` in the configuration it is
uvicorn, serving the stream endpoints on its event loop and everything else
through the same WSGI application in a thread pool (`digitalbrain/asgi.py`), so
a streamed answer no longer holds a request thread.
"""

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
parser.add_argument(
    "--no-worker",
    action="store_true",
    help=(
        "Serve requests only. The background lanes then run in their own process "
        "under `manage.py run_worker`."
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
#: `digitalbrain/servers.py` applies it to either server.
from digitalbrain.servers import (  # noqa: E402
    uvicorn_application,
    uvicorn_options,
    waitress_options,
)
from platform_core.document_worker import run_document_worker  # noqa: E402

#: By default the lanes run in this process, as they always have. With
#: --no-worker they do not, and `manage.py run_worker` runs them elsewhere. Chat
#: answers are not a lane: each one runs on a thread its stream request starts,
#: so they stay here either way and Stop needs nothing shared to reach them.
if not args.no_worker:
    Thread(target=run_document_worker, name="document-conversion", daemon=True).start()

from django.conf import settings  # noqa: E402

if settings.WEB_SERVER == "uvicorn":
    import uvicorn

    uvicorn.run(
        uvicorn_application(trusted_proxy=args.trusted_proxy),
        host=args.host,
        port=args.port,
        **uvicorn_options(),
    )
    sys.exit(0)

serve(
    application,
    host=args.host,
    port=args.port,
    **waitress_options(args.trusted_proxy),
)
