"""Record graph versions saved before their elements were rows, or check none is left."""

from django.core.management.base import BaseCommand, CommandError

from platform_core import graph_store


class Command(BaseCommand):
    help = "Write each graph version's sources, nodes and edges as rows (see graph_store)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--check",
            action="store_true",
            help="Fail if a version that could be recorded is not, or a recorded one "
            "does not read back exactly. Writes nothing.",
        )

    def handle(self, *args, check=False, **options):
        if check:
            behind = len(graph_store.behind())
            wrong = graph_store.mismatched()
            if behind or wrong:
                raise CommandError(
                    f"{behind} graph versions not recorded, {len(wrong)} not reading back exactly."
                )
            self.stdout.write("Every graph version that can be is recorded, and reads back.")
            return
        done = graph_store.backfill()
        self.stdout.write(f"Recorded {done} graph versions.")
