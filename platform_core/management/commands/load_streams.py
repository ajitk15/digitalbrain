"""Load-test the answer streams under waitress and uvicorn (`platform_core/load_streams.py`).

Throwaway database, no model call. Give `--redis host:port` a throwaway Redis
with no password - never a deployment's - or "fake" for fakeredis, which is
in-process and slower than a real server.
"""

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from platform_core import load_streams

COLUMNS = (
    ("server", "server", 9),
    ("readers", "readers", 8),
    ("answers", "answers", 8),
    ("accepted", "accepted", 9),
    ("refused", "refused", 8),
    ("unanswered", "no answer", 10),
    ("receiving", "receiving", 10),
    ("admitted_p95_ms", "admit p95", 10),
    ("first_word_p95_ms", "1st word p95", 13),
    ("pages", "pages", 7),
    ("page_p50_ms", "page p50", 9),
    ("page_p95_ms", "page p95", 9),
    ("page_max_ms", "page max", 9),
    ("page_failures", "page fail", 10),
    ("peak_threads", "threads", 8),
    ("errors_logged", "errors", 7),
)


class Command(BaseCommand):
    help = "Measure how many answer streams each web server follows, and page latency meanwhile."

    def add_arguments(self, parser):
        parser.add_argument("--readers", default="12,100,200",
                            help="Readers per stage, comma-separated.")
        parser.add_argument("--answers", type=int, default=12,
                            help="Answers per stage; readers are spread over them.")
        parser.add_argument("--seconds", type=int, default=30, help="Length of each stage.")
        parser.add_argument("--servers", default=",".join(load_streams.SERVERS))
        parser.add_argument("--redis", default="fake",
                            help='A throwaway Redis as host:port, or "fake".')
        parser.add_argument("--json", help="Also write the stages to this file.")

    def handle(self, *args, **options):
        try:
            readers = tuple(int(part) for part in options["readers"].split(","))
        except ValueError as failure:
            raise CommandError("--readers is a comma-separated list of numbers.") from failure
        servers = tuple(part.strip() for part in options["servers"].split(","))
        unknown = set(servers) - set(load_streams.SERVERS)
        if unknown:
            raise CommandError(f"Unknown server: {', '.join(sorted(unknown))}.")
        self.stdout.write("  ".join(title.rjust(width) for _, title, width in COLUMNS))

        def report(summary):
            self.stdout.write("  ".join(
                ("-" if summary[key] is None else str(summary[key])).rjust(width)
                for key, _, width in COLUMNS
            ))

        try:
            found = load_streams.run(
                readers=readers,
                answers=options["answers"],
                seconds=options["seconds"],
                servers=servers,
                redis_at=options["redis"],
                report=report,
            )
        except ValueError as failure:
            raise CommandError(str(failure)) from failure
        if options["json"]:
            Path(options["json"]).write_text(json.dumps(found, indent=2), encoding="utf-8")
