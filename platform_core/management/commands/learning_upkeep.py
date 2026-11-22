"""How "Draft a fix" is doing on Learned questions (free: it calls no model).

    manage.py learning_upkeep <application id>
    manage.py learning_upkeep <application id> --days 90

Counts the questions decided in the window: drafted ones against those decided
with no draft (rules alone), how many of each were accepted, and how many
drafted ones were accepted with the source the draft proposed. See
`platform_core/learning_draft.py`.
"""

from django.core.management.base import BaseCommand, CommandError

from platform_core import learning_draft
from platform_core.models import Application


def share(part, whole):
    return f"{round(100 * part / whole)}%" if whole else "-"


class Command(BaseCommand):
    help = "Accept rates of drafted Learned questions against undrafted ones."

    def add_arguments(self, parser):
        parser.add_argument("application", help="Application id")
        parser.add_argument("--days", type=int, default=30)

    def handle(self, *args, **options):
        app = Application.objects.filter(pk=options["application"] or None).first()
        if app is None:
            raise CommandError("Name an application.")
        if not 1 <= options["days"] <= 365:
            raise CommandError("--days must be between 1 and 365.")
        found = learning_draft.report(app, options["days"])
        for key, label in (("drafted", "Drafted"), ("rules", "No draft")):
            row = found[key]
            line = (
                f"{label}: {row['decided']} decided, {row['accepted']} accepted "
                f"({share(row['accepted'], row['decided'])})"
            )
            if key == "drafted":
                line += (
                    f", {row['as_drafted']} with the drafted source "
                    f"({share(row['as_drafted'], row['accepted'])} of those accepted)"
                )
            self.stdout.write(line)
        if found["drafted"]["decided"] < 10:
            self.stdout.write("Fewer than ten drafted questions decided: too few to judge yet.")
