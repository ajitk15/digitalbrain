from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from platform_core.models import ApplicationFeature, FeatureSwitch, User
from platform_core.policy import require_platform_admin
from platform_core.services import FEATURES, audit


class Command(BaseCommand):
    help = "Enable implemented features globally and reset existing application overrides."

    def add_arguments(self, parser):
        parser.add_argument("--actor", required=True)

    @transaction.atomic
    def handle(self, *args, **options):
        try:
            user = User.objects.get(username=options["actor"], is_active=True)
        except User.DoesNotExist:
            raise CommandError("Active administrator not found.") from None
        require_platform_admin(user)
        keys = [key for key, (_, available) in FEATURES.items() if available]
        for key in keys:
            FeatureSwitch.objects.update_or_create(key=key, defaults={"enabled": True})
            ApplicationFeature.objects.filter(key=key).update(enabled=True)
        audit(user, "features.enabled_all", "platform", details={"features": keys})
        self.stdout.write(f"Enabled {len(keys)} implemented features.")
