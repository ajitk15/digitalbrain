"""Run the background worker lanes in a process of their own.

`scripts/serve.py` runs every lane inside the web process unless it is started
with `--no-worker`. This is the other half: the same lanes, apart from the web
listener, so a deploy of one does not stop the other and a long provider call
does not share a process with sign-in.

Each lane is held by `document_worker.LaneKeeper`, so starting this beside a web
process that still runs its own lanes, or twice, runs nothing twice: the second
holder of a lane stands by and takes over when the first one goes.
"""

import logging
import signal
import threading
import time

from django.core.management.base import BaseCommand, CommandError

from platform_core.document_worker import LANES, lanes_named, start_lanes

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run background worker lanes apart from the web process."

    def add_arguments(self, parser):
        parser.add_argument(
            "--lanes",
            default="",
            help="Comma-separated lane names. Every lane when omitted. "
            f"Lanes: {', '.join(name for name, _, _ in LANES)}.",
        )
        parser.add_argument(
            "--grace",
            type=int,
            default=50,
            help="Seconds a step in progress may take to finish after a stop signal.",
        )

    def handle(self, *args, lanes="", grace=50, **options):
        names = [name.strip() for name in lanes.split(",") if name.strip()]
        try:
            selected = lanes_named(names or [name for name, _, _ in LANES])
        except ValueError as failure:
            raise CommandError(str(failure)) from None
        if grace < 0:
            raise CommandError("--grace cannot be negative.")
        stop = threading.Event()

        def request_stop(signum, frame):
            stop.set()

        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, request_stop)
        threads = start_lanes(selected, stop)
        chosen = ", ".join(name for name, _, _ in selected)
        self.stdout.write(f"worker: running {chosen}")
        logger.info(
            "worker started", extra={"event": "worker_started", "count": len(selected)}
        )
        while not stop.wait(1):
            if not any(thread.is_alive() for thread in threads):
                break
        stop.set()
        # A stop is honoured between steps: whatever started is let finish, up
        # to the grace period, and anything still running after it is left to
        # the reclaim each lane already performs for a worker that went away.
        deadline = time.monotonic() + grace
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        unfinished = [thread.name for thread in threads if thread.is_alive()]
        logger.info(
            "worker stopped", extra={"event": "worker_stopped", "count": len(unfinished)}
        )
        if unfinished:
            self.stdout.write(f"worker: stopped with work still running in {', '.join(unfinished)}")
        else:
            self.stdout.write("worker: stopped")
