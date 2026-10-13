"""Structured per-test receipts, including failures and explicit skipped coverage."""

import json
import os
import time
from pathlib import Path
from unittest.runner import TextTestResult

from django.test.runner import DiscoverRunner


class ReceiptResult(TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.receipts = []
        self.begun = time.monotonic()

    def startTest(self, test):
        self.begun = time.monotonic()
        super().startTest(test)

    def record(self, test, status, detail=""):
        self.receipts.append({"test": test.id(), "status": status,
                              "seconds": round(time.monotonic() - self.begun, 3),
                              "detail": detail[:3000]})

    def addSuccess(self, test):
        self.record(test, "passed")
        super().addSuccess(test)

    def addFailure(self, test, err):
        self.record(test, "failed", self._exc_info_to_string(err, test))
        super().addFailure(test, err)

    def addError(self, test, err):
        self.record(test, "error", self._exc_info_to_string(err, test))
        super().addError(test, err)

    def addSkip(self, test, reason):
        self.record(test, "skipped", reason)
        super().addSkip(test, reason)

    def addSubTest(self, test, subtest, err):
        if err:
            self.record(subtest, "failed", self._exc_info_to_string(err, test))
        super().addSubTest(test, subtest, err)


class ServiceOpsRunner(DiscoverRunner):
    def get_resultclass(self):
        return ReceiptResult

    def run_suite(self, suite, **kwargs):
        result = super().run_suite(suite, **kwargs)
        Path(os.environ["SERVICEOPS_TEST_REPORT"]).write_text(
            json.dumps({"tests": result.receipts, "successful": result.wasSuccessful(),
                        "count": result.testsRun}), encoding="utf-8")
        return result
