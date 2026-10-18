"""Apply migrations once, however many containers start at the same moment.

`migrate` takes no lock of its own: two processes running it together both read
the same pending list and both try to apply it, and the second fails half way
or, worse, does not. That is what held the web container to one replica. This
holds a PostgreSQL session advisory lock, on a connection of its own, for the
whole run, so a second caller waits and then finds nothing left to do. The
one-shot `migrate` service in docker-compose.scale.yaml is the normal way in;
the lock is what makes any other way safe too.

When the configuration names a `runtime_user`, the same run creates or
corrects that role and grants it every table, still under the lock
(`database_roles.ensure`): the role everything else connects as exists, with
access to every table this release has, before anything connects as it.

SQLite is one machine's file, with no second process to race: it just migrates.
"""

import hashlib
import time

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import OperationalError, connection, connections

#: The advisory lock number: the same in every process and release, and apart
#: from every lane's (`document_worker.lane_key`).
KEY = int.from_bytes(hashlib.sha256(b"digitalbrain-migrations").digest()[:8], "big", signed=True)
WAIT_SECONDS = 900
POLL_SECONDS = 2
#: The connection keeps the deployment's `lock_timeout`, so a migration that
#: needs a table lock while requests hold it gives up after five seconds rather
#: than queuing every request behind it. Each migration is one transaction on
#: PostgreSQL, so giving up undoes it whole and trying again is safe.
LOCK_ATTEMPTS = 5
LOCK_BACKOFF_SECONDS = 5
#: SQLSTATE lock_not_available, which lock_timeout raises.
LOCK_NOT_AVAILABLE = "55P03"


def lock_not_available(failure):
    cause = failure.__cause__ or failure.__context__
    return getattr(cause, "sqlstate", None) == LOCK_NOT_AVAILABLE


def migrate(write, verbosity=1):
    """Run `migrate`, trying again when a table lock was busy - only then."""
    for attempt in range(1, LOCK_ATTEMPTS + 1):
        try:
            call_command("migrate", interactive=False, verbosity=verbosity)
            return attempt
        except OperationalError as failure:
            if not lock_not_available(failure) or attempt == LOCK_ATTEMPTS:
                raise
            write(f"A table was in use; trying again ({attempt} of {LOCK_ATTEMPTS - 1}).")
            time.sleep(LOCK_BACKOFF_SECONDS * attempt)


class Command(BaseCommand):
    help = "Apply migrations under a lock, so that only one process ever applies them."

    def add_arguments(self, parser):
        parser.add_argument(
            "--wait",
            type=int,
            default=WAIT_SECONDS,
            help="Seconds to wait for another process applying migrations (default 900).",
        )

    def handle(self, *args, wait=WAIT_SECONDS, verbosity=1, **options):
        if connection.vendor != "postgresql":
            migrate(self.stdout.write, verbosity)
            return
        holder = connections.create_connection("default")
        try:
            self.acquire(holder, wait)
            # The deployment's statement_timeout (10 s) is right for a request
            # and wrong for building an index on a large table. Lifted for this
            # session only; lock_timeout stays, see LOCK_ATTEMPTS.
            with connection.cursor() as cursor:
                cursor.execute("SET statement_timeout = 0")
            migrate(self.stdout.write, verbosity)
            if settings.DATABASE_RUNTIME_USER:
                self.runtime_role(settings.DATABASE_RUNTIME_USER)
        finally:
            try:
                with holder.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_unlock(%s)", [KEY])
            except Exception:
                pass
            holder.close()

    def runtime_role(self, role):
        from digitalbrain.configuration import read_secret
        from platform_core import database_roles

        password = read_secret(settings.SECRET_DIRECTORY, "database_runtime_password")
        database_roles.ensure(role, password)
        self.stdout.write(f"The runtime role {role} can read and write rows, and nothing more.")

    def acquire(self, holder, wait):
        deadline = time.monotonic() + wait
        said = False
        while True:
            with holder.cursor() as cursor:
                cursor.execute("SELECT pg_try_advisory_lock(%s)", [KEY])
                if cursor.fetchone()[0]:
                    return
            if not said:
                said = True
                self.stdout.write("Another process is applying migrations; waiting for it.")
            if time.monotonic() >= deadline:
                raise CommandError(
                    f"Another process has been applying migrations for over {wait} seconds."
                )
            time.sleep(POLL_SECONDS)
