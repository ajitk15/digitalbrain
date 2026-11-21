"""Compare Code Factory's implementation agent with tools and without (charges apply).

    manage.py factory_pack <application id> --as <username>
    manage.py factory_pack <application id> --as <username> --record
    manage.py factory_pack <application id> --as <username> --only tools

Each ticket of benchmarks/factory-pack.json is sent through the application's
Code Factory configuration, credential and monthly budget, receipted as usual.
See `platform_core/factory_pack.py`.
"""

from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.http import Http404

from platform_core import factory_pack
from platform_core.models import Application, User


class Command(BaseCommand):
    help = "Compare Code Factory's implementation with tools and without (charges apply)."

    def add_arguments(self, parser):
        parser.add_argument("application", help="Application id")
        parser.add_argument("--as", dest="actor", help="Username the calls are made and charged as")
        parser.add_argument("--record", action="store_true", help="Save under benchmarks/factory/")
        parser.add_argument(
            "--only", choices=factory_pack.MODES, help="Run one way only (no comparison)"
        )

    def handle(self, *args, **options):
        app = Application.objects.filter(pk=options["application"] or None).first()
        user = User.objects.filter(username=options["actor"] or "", is_active=True).first()
        if app is None or user is None:
            raise CommandError("Name an application and --as an active user, who is charged.")
        modes = (options["only"],) if options["only"] else factory_pack.MODES
        try:
            record = factory_pack.run(user, app, modes)
        except (ValidationError, ImproperlyConfigured) as failure:
            raise CommandError(" ".join(getattr(failure, "messages", [str(failure)]))) from None
        except (PermissionDenied, Http404):
            raise CommandError(f"{user.username} may not run Code Factory here.") from None
        for row in record["details"]:
            mark = "ok " if row["fixed"] else "-- "
            said = row["failure"] or (
                f"changed {', '.join(row['changed']) or 'nothing'}; "
                f"saw {len(row['needs_seen'])}/{len(row['needs'])} dependencies"
            )
            self.stdout.write(f"{mark}{row['ticket']} {row['mode']}: {said}")
        for mode, found in record["metrics"].items():
            self.stdout.write(
                f"{mode}: fixed {found['fixed']}/{found['tickets']}, checks met "
                f"{found['checks_met']}%, dependencies seen {found['needs_seen']}%, "
                f"{found['turns']} turns, {found['tool_calls']} reads, ${found['cost']}, "
                f"{found['seconds']} s"
            )
        self.stdout.write(factory_pack.verdict(record["metrics"]))
        if options["record"]:
            self.stdout.write(f"Recorded {factory_pack.save(record)}")
