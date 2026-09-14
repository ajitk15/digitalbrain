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
args = parser.parse_args()
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
)
