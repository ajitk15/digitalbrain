"""Make the audit table's months ahead and drop those past the window (partitions.py).

`apply_migrations` does this on every deploy. Run it on a schedule as well
where deploys are months apart, with the configuration that connects as the
owner - a partition is a table, and the runtime role cannot make one.
`--check` only reads, so it runs as any role: it exits non-zero when fewer
than `MIN_AHEAD` months are made or rows are waiting in a default partition.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, connection

from platform_core import partitions


class Command(BaseCommand):
    help = "Make the audit table's monthly partitions ahead; drop those past the retention window."

    def add_arguments(self, parser):
        parser.add_argument(
            "--check", action="store_true", help="Only report; exit non-zero on a problem."
        )

    def handle(self, *args, check=False, **options):
        if connection.vendor != "postgresql":
            self.stdout.write("Only PostgreSQL partitions the audit table; nothing to do.")
            return
        if check:
            found = partitions.status()
            for table, figures in found.items():
                waiting = "rows waiting" if figures["waiting"] else "empty"
                self.stdout.write(
                    f"{table}: {figures['ahead']} months ahead, default partition {waiting}"
                )
            said = partitions.problems(found)
            if said:
                raise CommandError("; ".join(said))
            return
        try:
            report = partitions.ensure(retention=settings.RETENTION)
        except DatabaseError as failure:
            raise CommandError(
                "Making a partition is DDL: run this with the configuration that connects "
                f"as the owner, as the migrate job does. ({type(failure).__name__})"
            ) from failure
        for line in describe(report):
            self.stdout.write(line)


def describe(report):
    for table, done in report.items():
        yield (
            f"{table}: made {len(done['made'])}, moved {done['moved']} rows out of the "
            f"default, dropped {len(done['dropped'])} past the retention window."
        )
