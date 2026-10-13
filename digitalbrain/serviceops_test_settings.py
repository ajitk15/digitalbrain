"""Child-suite settings: disposable SQLite, no provider/connector credentials."""

import os

from .settings import *  # noqa: F403

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
SECRET_DIRECTORY = os.environ["SERVICEOPS_TEST_DIRECTORY"]
MANAGED_SECRET_DIRECTORY = ""
CONNECTOR_SECRET_DIRECTORY = ""
CLAUDE_USE_HOST_LOGIN = False
DOCUMENT_AUTO_CONVERT = False
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
TEST_RUNNER = "digitalbrain.serviceops_test_runner.ServiceOpsRunner"
