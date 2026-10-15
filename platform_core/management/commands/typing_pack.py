"""Measure the AI document-typing fallback on a frozen, labelled pack (charges apply).

    manage.py typing_pack --build-pack
    manage.py typing_pack <application id> --as <username>
    manage.py typing_pack <application id> --as <username> --record

One call per item through the application's Document typing configuration,
credential and monthly budget, receipted as usual. See
`platform_core/typing_pack.py`.
"""

from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.http import Http404

from platform_core import typing_pack
from platform_core.models import Application, User


class Command(BaseCommand):
    help = "Measure the AI document-typing fallback on a labelled pack (charges apply)."

    def add_arguments(self, parser):
        parser.add_argument("application", nargs="?", help="Application id")
        parser.add_argument("--as", dest="actor", help="Username the calls are made and charged as")
        parser.add_argument("--record", action="store_true", help="Save under benchmarks/typing/")
        parser.add_argument("--build-pack", action="store_true", help="Rebuild the frozen pack")

    def handle(self, *args, **options):
        if options["build_pack"]:
            pack = typing_pack.build_pack()
            labels = sorted({item["label"] for item in pack["items"]})
            self.stdout.write(
                f"Built {typing_pack.PACK.name}: {len(pack['items'])} items the rules leave "
                f"as Other, labelled {', '.join(labels)}."
            )
            if not options["application"]:
                return
        app = Application.objects.filter(pk=options["application"] or None).first()
        user = User.objects.filter(username=options["actor"] or "", is_active=True).first()
        if app is None or user is None:
            raise CommandError("Name an application and --as an active user, who is charged.")
        try:
            record = typing_pack.run(user, app)
        except (ValidationError, ImproperlyConfigured) as failure:
            raise CommandError(" ".join(getattr(failure, "messages", [str(failure)]))) from None
        except (PermissionDenied, Http404):
            raise CommandError(f"{user.username} may not use this application's models.") from None
        for item in record["details"]:
            proposed = f", proposed {item['proposed']}" if item["proposed"] else ""
            said = item["key"] if item["verdict"] == "accepted" else (
                f"other ({item['reason']}{proposed})"
            )
            mark = "ok " if (item["verdict"] == "accepted" and item["key"] == item["label"]) or (
                item["label"] == "other" and item["verdict"] != "accepted") else "-- "
            self.stdout.write(f"{mark}{item['id']}: labelled {item['label']}, got {said}")
        metrics = record["metrics"]
        self.stdout.write(
            f"{record['model']}: {metrics['items']} items, recall {metrics['recall']}%, "
            f"precision {metrics['precision']}%, {metrics['wrong_type']} wrong type, "
            f"{metrics['false_positive_on_other']} false positive on Other, "
            f"{metrics['quote_refused']} refused for a quote."
        )
        if options["record"]:
            self.stdout.write(f"Recorded {typing_pack.save(record)}")
