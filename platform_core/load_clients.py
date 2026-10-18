"""The browsers for `manage.py load_streams`: readers and page requests, in a process of their own.

Run as `python -m platform_core.load_clients` with the stage as JSON on stdin;
the stage's figures come back as JSON on stdout. It imports nothing from
Django and nothing from the platform beyond this file, so its own work - a
reader parses every line an answer sends - is not counted against the server
it measures, as it was when both shared one interpreter.
"""

import asyncio
import json
import sys
import time
from dataclasses import asdict, dataclass, field

#: Page requests in flight at once, each opening a new connection.
PAGE_CLIENTS = 4
#: Seconds a reader or page request may wait for a status line.
ANSWER_SECONDS = 15


@dataclass
class Stage:
    server: str
    readers: int
    answers: int
    accepted: int = 0
    refused: dict = field(default_factory=dict)
    unanswered: int = 0
    receiving: int = 0
    deltas: int = 0
    admitted_ms: list = field(default_factory=list)
    first_delta_ms: list = field(default_factory=list)
    pages_ms: list = field(default_factory=list)
    page_failures: int = 0
    peak_threads: int = 0
    thread_kinds: dict = field(default_factory=dict)
    errors_logged: int = 0
    first_errors: list = field(default_factory=list)

    def summary(self):
        return {
            "server": self.server,
            "readers": self.readers,
            "answers": self.answers,
            "accepted": self.accepted,
            "refused": sum(self.refused.values()),
            "unanswered": self.unanswered,
            "receiving": self.receiving,
            "admitted_p95_ms": percentile(self.admitted_ms, 95),
            "first_word_p95_ms": percentile(self.first_delta_ms, 95),
            "words": self.deltas,
            "pages": len(self.pages_ms),
            "page_p50_ms": percentile(self.pages_ms, 50),
            "page_p95_ms": percentile(self.pages_ms, 95),
            "page_max_ms": round(max(self.pages_ms), 1) if self.pages_ms else None,
            "page_failures": self.page_failures,
            "peak_threads": self.peak_threads,
            "thread_kinds": self.thread_kinds,
            "errors_logged": self.errors_logged,
            "first_errors": self.first_errors,
        }


def percentile(values, pct):
    """The nearest-rank percentile, or None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, round(pct / 100 * len(ordered)))
    return round(ordered[min(rank, len(ordered)) - 1], 1)


async def status_of(reader):
    line = await asyncio.wait_for(reader.readline(), ANSWER_SECONDS)
    parts = line.split()
    return int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0


def request(path, cookie, close=False):
    connection = "Connection: close\r\n" if close else ""
    return (
        f"GET {path} HTTP/1.1\r\nHost: localhost\r\nCookie: {cookie}\r\n"
        f"Accept: text/event-stream\r\n{connection}\r\n"
    ).encode()


async def follow(port, path, cookie, until, stage, began):
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port), ANSWER_SECONDS
        )
    except (TimeoutError, OSError):
        stage.unanswered += 1
        return
    try:
        writer.write(request(path, cookie))
        try:
            status = await status_of(reader)
        except (TimeoutError, OSError):
            stage.unanswered += 1
            return
        if status != 200:
            stage.refused[str(status)] = stage.refused.get(str(status), 0) + 1
            return
        stage.accepted += 1
        stage.admitted_ms.append((time.monotonic() - began) * 1000)
        received = False
        while (left := until - time.monotonic()) > 0:
            try:
                line = await asyncio.wait_for(reader.readline(), left)
            except (TimeoutError, OSError):
                break
            if not line:
                break
            if line.startswith(b"event: delta"):
                if not received:
                    stage.first_delta_ms.append((time.monotonic() - began) * 1000)
                received = True
                stage.deltas += 1
        stage.receiving += received
    finally:
        writer.close()


async def page(port, path, cookie, until, stage):
    while time.monotonic() < until:
        began = time.monotonic()
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", port), ANSWER_SECONDS
            )
            try:
                writer.write(request(path, cookie, close=True))
                status = await status_of(reader)
                await asyncio.wait_for(reader.read(), ANSWER_SECONDS)
            finally:
                writer.close()
        except (TimeoutError, OSError):
            status = 0
        if status == 200:
            stage.pages_ms.append((time.monotonic() - began) * 1000)
        else:
            stage.page_failures += 1
            await asyncio.sleep(0.05)


async def later(delay, job):
    await asyncio.sleep(delay)
    await job()


async def drive(spec, stage):
    """Readers spread over the answers, the first reader of each starting it.

    Every answer is started before anyone else follows it, as in a browser:
    the tab that asked opens the stream first. The other readers then arrive
    evenly over the first half of the stage - a reconnect or a second tab
    follows an answer some time after it began, not all in one instant - and
    `admitted_ms` says how long each waited to be let in.
    """
    port, cookie, streams = spec["port"], spec["cookie"], spec["streams"]
    seconds = spec["seconds"]
    began = time.monotonic()
    until = began + seconds
    tasks = [asyncio.ensure_future(follow(port, path, cookie, until, stage, began))
             for path in streams]
    others = stage.readers - len(streams)
    spread = seconds / 2
    for number in range(others):
        path = streams[(len(streams) + number) % len(streams)]
        tasks.append(asyncio.ensure_future(later(
            1 + spread * number / max(others, 1),
            lambda path=path: follow(port, path, cookie, until, stage, time.monotonic()),
        )))
    tasks += [asyncio.ensure_future(page(port, spec["page"], cookie, until, stage))
              for _ in range(PAGE_CLIENTS)]
    await asyncio.gather(*tasks)


def main():
    spec = json.loads(sys.stdin.read())
    stage = Stage(server=spec["server"], readers=spec["readers"], answers=len(spec["streams"]))
    asyncio.run(drive(spec, stage))
    sys.stdout.write(json.dumps(asdict(stage)))


if __name__ == "__main__":
    main()
