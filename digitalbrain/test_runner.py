"""The test runner: Django's own, minus machine-local shared credentials."""

from django.conf import settings
from django.test.runner import DiscoverRunner


class HermeticRunner(DiscoverRunner):
    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        # A test that wants the shared folder sets it with override_settings.
        settings.CONNECTOR_SECRET_DIRECTORY = ""
