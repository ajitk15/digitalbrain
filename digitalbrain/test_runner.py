"""The test runner: Django's own, minus machine-local shared credentials."""

from django.conf import settings
from django.test.runner import DiscoverRunner, ParallelTestSuite, _init_worker


def hermetic(settings_holder):
    # A test that wants the shared folder sets it with override_settings.
    settings_holder.CONNECTOR_SECRET_DIRECTORY = ""
    # Tests mount made-up Claude credentials and patch the CLI adapter; one
    # that means the Messages API turns it on with override_settings.
    settings_holder.CLAUDE_MESSAGES_API = False
    # A developer's `[originals]` bucket must not receive test uploads; a test
    # of the bucket store names one with override_settings.
    settings_holder.ORIGINALS = {}


def init_worker(*args, **kwargs):
    """A `--parallel` worker, which on Windows loads settings afresh.

    Spawned workers never run the runner's own `setup_test_environment`, so
    without this every one read the developer's shared connector folder.
    """
    _init_worker(*args, **kwargs)
    hermetic(settings)


class HermeticSuite(ParallelTestSuite):
    init_worker = init_worker


class HermeticRunner(DiscoverRunner):
    parallel_test_suite = HermeticSuite

    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        hermetic(settings)
