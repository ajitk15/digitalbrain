"""Keep a claimed row looking alive while its work runs.

Several lanes find work a stopped worker left by its age: a repository still
"indexing" ten minutes after it was last touched, a review still "reading"
after fifteen. With one process per lane that age could only belong to a dead
worker, since the lane ran one step at a time. With every process running the
lane, a second one could read a slow index as an abandoned one, put it back,
and do it again - twice the clone, and for an AI review, twice the bill.

`KeepAlive` touches the row's timestamp every `EVERY` seconds while its block
runs, on a thread of its own, so age measures a worker that stopped, never one
that is slow. It is handed the claimed row's own filter (its id, its status and
its job token), so it touches nothing until the claim lands and nothing after
the row is finished, put back or claimed afresh. Like `jobs.Renewal` it waits a
whole interval before its first write, so short work never writes from a
second thread at all.
"""

import logging
import threading

from django.db import connection
from django.utils import timezone

logger = logging.getLogger(__name__)

#: Seconds between touches. Every stale window it guards is ten minutes or more.
EVERY = 60


class KeepAlive:
    def __init__(self, rows, field="updated_at", every=EVERY):
        self.rows = rows
        self.field = field
        self.every = every
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name="keepalive", daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join(timeout=5)

    def run(self):
        try:
            while not self.stop.wait(self.every):
                self.rows.update(**{self.field: timezone.now()})
        except Exception as failure:
            logger.warning(
                "keepalive failed",
                extra={
                    "event": "keepalive_failed",
                    "model": self.rows.model.__name__,
                    "exception_type": type(failure).__name__,
                },
            )
        finally:
            connection.close()
