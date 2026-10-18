"""Print what each worker lane has waiting and whether a process is running it.

Counts, ages and the process that holds each lane - nothing about any record.
`--check` exits non-zero when a lane has no live heartbeat, for a health probe
on a worker container.
"""

import json

from django.core.management.base import BaseCommand, CommandError

from platform_core.queue_status import lane_status


def age(seconds):
    if seconds is None:
        return "-"
    if seconds < 120:
        return f"{seconds}s"
    if seconds < 7200:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"


class Command(BaseCommand):
    help = "Show each worker lane's waiting work and heartbeat."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="One JSON object per line.")
        parser.add_argument(
            "--check",
            action="store_true",
            help="Exit with an error when any lane has no live heartbeat.",
        )
        parser.add_argument(
            "--lanes",
            default="",
            help="Comma-separated lanes --check should require. Every lane when omitted.",
        )

    def handle(self, *args, **options):
        rows = lane_status()
        wanted = {name.strip() for name in options["lanes"].split(",") if name.strip()}
        known = {row["lane"] for row in rows}
        if wanted - known:
            raise CommandError(f"Unknown lane: {', '.join(sorted(wanted - known))}.")
        if options["json"]:
            for row in rows:
                self.stdout.write(json.dumps(row, separators=(",", ":")))
        else:
            self.stdout.write(
                f"{'lane':<18}{'waiting':>8}{'oldest':>9}{'dead':>6}{'beat':>8}  owner"
            )
            for row in rows:
                self.stdout.write(
                    f"{row['lane']:<18}{row['waiting']:>8}{age(row['oldest_seconds']):>9}"
                    f"{row['dead']:>6}{age(row['heartbeat_seconds']):>8}  "
                    f"{row['owner'] or '-'}{'  STALE' if row['stale'] else ''}"
                )
        if options["check"]:
            required = wanted or known
            stale = [row["lane"] for row in rows if row["stale"] and row["lane"] in required]
            if stale:
                raise CommandError(f"No live worker for: {', '.join(stale)}.")
