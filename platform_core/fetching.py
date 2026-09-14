"""Hardened retrieval of a user-supplied URL.

Fetching an address a user typed means this server makes an outbound request to a
destination they choose, which is the classic server-side request forgery shape.
Everything in this module exists to bound that:

* Only http and https. No file:, ftp:, gopher:, data: or anything else.
* The hostname is resolved first and **every** resolved address is checked. A name
  that resolves to loopback, a private range, link-local (which is where the cloud
  metadata endpoint 169.254.169.254 lives), multicast or any reserved block is
  refused unless an operator has explicitly allow-listed that host.
* The connection is then **pinned to the address that was validated**. Resolving
  and connecting separately would leave a window in which DNS could answer
  differently the second time, which is how rebinding attacks work. TLS still
  validates against the hostname, so pinning costs no certificate safety.
* Redirects are never followed. A redirect is an unvalidated second destination;
  the caller is told where it pointed instead.
* Responses are bounded in bytes and in time, and nothing is streamed to disk
  until the size is known to be acceptable.
* No credential, cookie or authorization header is ever attached by this module.
  Callers that need one pass it explicitly.

The bytes this returns are stored and converted exactly like an uploaded file, by
the existing offline pipeline. MarkItDown still never sees a URL.
"""

import http.client
import ipaddress
import re
import socket
import ssl
from urllib.parse import unquote, urlparse

MAX_BYTES = 8 * 1024 * 1024
TIMEOUT = 20
USER_AGENT = "Digital-Brain"

#: Extensions MarkItDown handles, chosen from the content type when the URL path
#: does not already carry one.
CONTENT_SUFFIXES = {
    "text/html": ".html",
    "application/xhtml+xml": ".html",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/csv": ".csv",
    "application/json": ".json",
    "application/xml": ".xml",
    "text/xml": ".xml",
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
}


class FetchError(Exception):
    """A URL could not be retrieved. The message is safe to show a user."""


def _blocked(address):
    """True for any address a user must not be able to aim this server at."""
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def allowed_hosts():
    """Internal hosts an operator has explicitly permitted, lower-cased."""
    from django.conf import settings

    return {host.lower() for host in getattr(settings, "FETCH_ALLOW_HOSTS", [])}


def resolve(host, port):
    """Every address `host` resolves to, refusing the ones a user must not reach.

    Returns the list of addresses so the caller can pin to one of them.
    """
    try:
        records = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError):
        raise FetchError(f"{host} could not be resolved.") from None
    if not records:
        raise FetchError(f"{host} could not be resolved.")

    permitted = host.lower() in allowed_hosts()
    addresses = []
    for record in records:
        literal = record[4][0]
        try:
            address = ipaddress.ip_address(literal)
        except ValueError:
            continue
        if _blocked(address) and not permitted:
            raise FetchError(
                f"{host} resolves to a private or reserved address. An operator can "
                "allow specific internal hosts in the deployment configuration."
            )
        addresses.append((record[0], literal))
    if not addresses:
        raise FetchError(f"{host} could not be resolved.")
    return addresses


def _connection(parsed, family, literal):
    """A connection pinned to one already-validated address.

    The host header and TLS server name stay the real hostname, so certificate
    verification is unaffected; only the address dialled is fixed.
    """
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if parsed.scheme == "https":
        connection = http.client.HTTPSConnection(
            parsed.hostname, port, timeout=TIMEOUT, context=ssl.create_default_context()
        )
    else:
        connection = http.client.HTTPConnection(parsed.hostname, port, timeout=TIMEOUT)

    def connect():
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(TIMEOUT)
        sock.connect((literal, port))
        if parsed.scheme == "https":
            sock = connection._context.wrap_socket(sock, server_hostname=parsed.hostname)
        connection.sock = sock

    connection.connect = connect
    return connection


def normalise(raw):
    """Validate a user-supplied URL and return its parsed form."""
    value = (raw or "").strip()
    if not value:
        raise FetchError("Enter a link.")
    if len(value) > 2000:
        raise FetchError("That link is too long.")
    # Detect a scheme before assuming https, or "data:text/html,..." becomes
    # "https://data:text/html,..." and slips past the scheme check entirely.
    # Dots are excluded from the pattern so "example.com:8080/x" still reads as a
    # bare host and port rather than a scheme named "example.com".
    scheme = re.match(r"^([a-zA-Z][a-zA-Z0-9+\-]*):", value)
    if scheme:
        if scheme.group(1).lower() not in {"http", "https"}:
            raise FetchError("Only http and https links can be imported.")
    else:
        value = "https://" + value
    # urlparse raises on some shapes (an unmatched IPv6 bracket) and defers others
    # until .hostname or .port is read (a port outside the valid range). Left
    # uncaught, either turned a mistyped link into a 500 rather than a message.
    try:
        parsed = urlparse(value)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        raise FetchError("That link is not a valid address.") from None
    if parsed.scheme not in {"http", "https"}:
        raise FetchError("Only http and https links can be imported.")
    if not host:
        raise FetchError("That link has no host.")
    if port is not None and not 1 <= port <= 65535:
        raise FetchError("That link has an invalid port.")
    if parsed.username or parsed.password:
        raise FetchError("Links containing credentials are not accepted.")
    return parsed


def suggested_name(parsed, content_type):
    """A filename for the stored copy, carrying a suffix MarkItDown understands."""
    path = unquote(parsed.path or "").rstrip("/")
    name = path.rsplit("/", 1)[-1] if path else ""
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.") or parsed.hostname
    if "." not in name:
        suffix = CONTENT_SUFFIXES.get((content_type or "").split(";")[0].strip().lower(), ".html")
        name += suffix
    return name[:180]


def fetch(raw, *, headers=None, method="GET", body=None):
    """Retrieve a URL. Returns (bytes, content_type, filename, final_url).

    `method`/`body` exist for the exchanges that cannot be a GET: trading OAuth
    client credentials for an access token, and the GitHub calls that open a pull
    request. They change nothing about the address checks - resolution, the
    private-address refusal, the pinned connection and the no-redirect rule all
    still apply, which is the whole reason nothing here opens its own socket.
    """
    if method not in {"GET", "POST", "PUT"}:
        raise FetchError("Unsupported request method.")
    parsed = normalise(raw)
    addresses = resolve(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    family, literal = addresses[0]
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query

    request_headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    request_headers.update(headers or {})

    connection = _connection(parsed, family, literal)
    try:
        if body is not None:
            request_headers.setdefault("Content-Length", str(len(body)))
        connection.request(method, target, body=body, headers=request_headers)
        response = connection.getresponse()
        if response.status in {301, 302, 303, 307, 308}:
            location = response.getheader("Location", "")
            raise FetchError(
                "That link redirects, which is not followed automatically. "
                + (f"Try {location[:200]} directly." if location else "Use the final address.")
            )
        # 201 is how an API reports something created; it is a success here.
        if response.status not in {200, 201}:
            raise FetchError(f"{parsed.hostname} returned HTTP {response.status}.")
        declared = response.getheader("Content-Length")
        if declared and declared.isdigit() and int(declared) > MAX_BYTES:
            raise FetchError("That document is larger than 8 MB.")
        body = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise FetchError("That document is larger than 8 MB.")
        if not body.strip():
            raise FetchError("That link returned nothing to import.")
        content_type = response.getheader("Content-Type", "")
    except FetchError:
        raise
    except (OSError, http.client.HTTPException, ssl.SSLError, ValueError):
        raise FetchError(
            f"{parsed.hostname} could not be reached. Check the link and connectivity."
        ) from None
    finally:
        connection.close()

    return body, content_type, suggested_name(parsed, content_type), parsed.geturl()
