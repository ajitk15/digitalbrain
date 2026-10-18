"""Redis, when a deployment configures one: what must cross processes, quickly.

Two things live here, and nothing else should:

* **Streamed answers** (`agent_runtime.streaming`). Each answer's events are a
  Redis stream, so any process can replay an answer from the start or from the
  last event a browser saw, and follow it while it is written. A dropped
  connection reconnects and carries on, on whichever web process it lands,
  instead of losing the answer. Stop is a list the producing process blocks
  on, so it reaches that process at once.
* **The API rate limit** (`api_auth.throttle`): one counter per token and
  window, shared by every web process.

Together they are what kept the web container at one replica. Without a
`[redis]` table nothing here is used and both stay process-local, as before.

Redis holds copies in transit, never a record: an answer's row is written by
its worker exactly as without Redis, and its events expire. The password is the
mounted file `redis_password`, like the database's - never in the config, never
in the environment. Production refuses a connection without verified TLS
(`load_config`).
"""

import threading
from pathlib import Path

from django.conf import settings

#: Every key this platform writes starts with this, so a shared Redis is safe.
PREFIX = "digitalbrain:"
#: Longer than the longest block a reader asks for (`streaming.HEARTBEAT_SECONDS`).
SOCKET_SECONDS = 30
CONNECT_SECONDS = 5

_client = None
_lock = threading.Lock()


def enabled():
    return bool(getattr(settings, "REDIS", None))


def key(*parts):
    return PREFIX + ":".join(str(part) for part in parts)


def password():
    """The mounted `redis_password`, or None in development when there is none."""
    from digitalbrain.configuration import read_secret

    directory = settings.SECRET_DIRECTORY
    if not settings.PRODUCTION and not (Path(directory) / "redis_password").is_file():
        return None
    return read_secret(directory, "redis_password")


def connect(config):
    import redis

    options = {
        "host": config["host"],
        "port": config.get("port", 6379),
        "db": config.get("db", 0),
        "username": config.get("username") or None,
        "password": password(),
        "socket_timeout": SOCKET_SECONDS,
        "socket_connect_timeout": CONNECT_SECONDS,
        "health_check_interval": 30,
        "decode_responses": True,
    }
    if config.get("tls"):
        options.update(ssl=True, ssl_cert_reqs="required", ssl_check_hostname=True)
        if config.get("ca_certificate"):
            options["ssl_ca_certs"] = config["ca_certificate"]
    return redis.Redis(**options)


def client():
    """One client per process; redis-py pools connections behind it."""
    global _client
    with _lock:
        if _client is None:
            _client = connect(settings.REDIS)
        return _client


def use(replacement):
    """Swap the client, for tests (a fakeredis client) - None to rebuild it."""
    global _client
    with _lock:
        _client = replacement


def count(name, seconds):
    """Increment a shared counter that lives `seconds`; returns the new value."""
    connection = client()
    pipe = connection.pipeline()
    pipe.incr(key(name))
    pipe.expire(key(name), seconds, nx=True)
    value, _ = pipe.execute()
    return int(value)
