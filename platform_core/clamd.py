"""Scanning a document through a running clamd rather than a new clamscan.

`clamscan` loads the whole signature database - about a gigabyte - every time
it starts, so each document paid seconds of start-up before a byte of it was
read, and conversions could only ever be as fast as that. A clamd daemon loads
it once and is sent each file over its socket with INSTREAM.

The rules `processing` holds for clamscan hold here too, because they are
about the scan, not the program:

* **Fail closed.** No answer, a refused connection, a timeout, a reply this
  module does not recognise, a file over the daemon's stream limit - each is
  "could not scan", and the document stays quarantined.
* **Signatures under a week old.** clamscan is run with
  `--fail-if-cvd-older-than=7`; clamd has no such switch, so the database date
  it reports for VERSION is checked before every scan. A daemon whose
  freshclam stopped updating fails closed exactly as a stale clamscan does.

The address is the operator's `clamd` setting - an absolute socket path, or
`host:port` - never anything a request supplies, so this is not a fetcher in
the sense `fetching.py` guards.
"""

import socket
import struct
from datetime import UTC, datetime, timedelta

from django.conf import settings

#: The same bound `--fail-if-cvd-older-than=7` sets for clamscan.
MAX_SIGNATURE_AGE = timedelta(days=7)
#: Seconds for one exchange with the daemon, the same as a clamscan run.
TIMEOUT = 30
CHUNK = 64 * 1024


class ScanUnavailable(Exception):
    """The file could not be scanned. Its reason is a fixed label, never daemon text."""


def configured():
    return bool(settings.CONFIG.get("clamd", ""))


def target(value=None):
    """("unix", path) or ("tcp", (host, port)) for a `clamd` setting."""
    value = (value if value is not None else settings.CONFIG.get("clamd", "")).strip()
    if value.startswith("/"):
        return "unix", value
    host, _, port = value.rpartition(":")
    if not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise ScanUnavailable("address")
    return "tcp", (host, int(port))


def connect(timeout=TIMEOUT):
    kind, address = target()
    if kind == "unix":
        if not hasattr(socket, "AF_UNIX"):
            raise ScanUnavailable("address")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(address)
        except OSError:
            sock.close()
            raise
        return sock
    return socket.create_connection(address, timeout=timeout)


def read_reply(sock):
    """One NUL-terminated reply, as text."""
    received = b""
    while not received.endswith(b"\0"):
        part = sock.recv(4096)
        if not part:
            break
        received += part
        if len(received) > 64 * 1024:
            raise ScanUnavailable("reply")
    return received.rstrip(b"\0").decode("utf-8", "replace").strip()


def signatures_dated(reply):
    """The database date in a VERSION reply: `ClamAV 1.4.1/27431/Fri Oct 17 08:23:55 2026`.

    A daemon that has loaded no database replies without the date, and that is
    a daemon that cannot vouch for anything.
    """
    parts = reply.split("/")
    if len(parts) < 3:
        raise ScanUnavailable("signatures")
    try:
        built = datetime.strptime(" ".join(parts[-1].split()), "%a %b %d %H:%M:%S %Y")
    except ValueError:
        raise ScanUnavailable("signatures") from None
    # clamd prints the daemon's local time without a zone. Read as UTC it is
    # at most a day out, against a bound of seven.
    return built.replace(tzinfo=UTC)


def check_signatures(now=None):
    sock = connect()
    try:
        sock.sendall(b"zVERSION\0")
        reply = read_reply(sock)
    finally:
        sock.close()
    built = signatures_dated(reply)
    if (now or datetime.now(UTC)) - built > MAX_SIGNATURE_AGE:
        raise ScanUnavailable("signatures")


def scan(path):
    """0 when the file is clean, 1 when a signature matched - clamscan's codes.

    Anything else raises ScanUnavailable, which the caller treats as the scanner
    being unavailable: the document stays quarantined and nothing is converted.
    """
    try:
        check_signatures()
        sock = connect()
        try:
            sock.sendall(b"zINSTREAM\0")
            with open(path, "rb") as source:
                while chunk := source.read(CHUNK):
                    sock.sendall(struct.pack("!L", len(chunk)) + chunk)
            sock.sendall(struct.pack("!L", 0))
            reply = read_reply(sock)
        finally:
            sock.close()
    except ScanUnavailable:
        raise
    except (OSError, ValueError):
        raise ScanUnavailable("connection") from None
    if reply == "stream: OK":
        return 0
    if reply.startswith("stream: ") and reply.endswith(" FOUND"):
        return 1
    # "INSTREAM size limit exceeded. ERROR", a daemon fault, anything unknown.
    raise ScanUnavailable("reply")
