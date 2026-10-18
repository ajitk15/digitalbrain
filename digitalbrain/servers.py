"""How each web server is run: one definition for `scripts/serve.py` and the load test.

`manage.py load_streams` measures the servers exactly as they are deployed, so
their options live here rather than inside the script that starts one.
"""

from platform_core.agent_runtime.streaming import MAX_ACTIVE_STREAMS, MAX_ASYNC_READERS

#: Under waitress a streaming answer holds its request thread until it finishes,
#: so the pool has to outnumber the streams that can exist - otherwise a full
#: complement of answers leaves nothing to serve the Stop button that would end
#: them. Under uvicorn streams do not use the pool, so it serves pages alone.
REQUEST_THREADS = MAX_ACTIVE_STREAMS * 2
#: Open connections either server accepts before refusing more.
CONNECTION_LIMIT = 100
MAX_REQUEST_HEADER = 32768
MAX_REQUEST_BODY = 23068672


def waitress_options(trusted_proxy=""):
    """Keyword arguments for `waitress.serve` / `create_server`, host and port aside.

    Waitress discards X-Forwarded-* from anyone it was not told to trust, which
    is the right default and the reason `--trusted-proxy` has to exist (see
    `scripts/serve.py`). Anything the proxy did not set is removed rather than
    passed through, so a header a client invented cannot survive one honest hop.
    """
    options = {
        "threads": REQUEST_THREADS,
        "connection_limit": CONNECTION_LIMIT,
        "channel_timeout": 60,
        "cleanup_interval": 10,
        "max_request_header_size": MAX_REQUEST_HEADER,
        "max_request_body_size": MAX_REQUEST_BODY,
        "expose_tracebacks": False,
        "ident": "DigitalBrain",
    }
    if trusted_proxy:
        options.update(
            trusted_proxy=trusted_proxy,
            trusted_proxy_count=1,
            trusted_proxy_headers={"x-forwarded-for", "x-forwarded-proto", "x-forwarded-host"},
            clear_untrusted_proxy_headers=True,
        )
    return options


def uvicorn_application(trusted_proxy=""):
    from .asgi import MAX_BODY, build

    assert MAX_BODY == MAX_REQUEST_BODY, "keep in step with waitress's max_request_body_size"
    return build(trusted_proxy=trusted_proxy, workers=REQUEST_THREADS)


def uvicorn_options():
    """Keyword arguments for `uvicorn.Config`, host and port aside."""
    return {
        # The router believes X-Forwarded-* from the trusted proxy only and
        # removes it from everyone else, as waitress did; uvicorn's own
        # handling would leave an untrusted header in place.
        "proxy_headers": False,
        "server_header": False,
        "ws": "none",
        "lifespan": "off",
        "access_log": False,
        "log_config": None,
        # Waitress's connection_limit, plus a slot per reader that may follow
        # an answer, and its max_request_header_size.
        "limit_concurrency": CONNECTION_LIMIT + MAX_ASYNC_READERS,
        "h11_max_incomplete_event_size": MAX_REQUEST_HEADER,
        "timeout_keep_alive": 5,
        "timeout_graceful_shutdown": 10,
    }
