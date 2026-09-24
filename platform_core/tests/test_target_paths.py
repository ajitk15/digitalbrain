"""The file paths a plan's targets name, with any symbol taken off.

A live run's design named its targets as "path:symbol". Left on, the suffix made
every file read as absent, so the implementation *created* files named
"…/errors.py:ConsentWithheld" and edited none of the real ones, and the test
author saw an extension it did not recognise and wrote no test at all.
"""

from types import SimpleNamespace

from django.test import SimpleTestCase

from platform_core.code_factory import derived_test_paths, target_paths


class Plan:
    def __init__(self, *target_lists):
        self._items = [
            SimpleNamespace(targets=list(targets), sequence=index, status="accepted")
            for index, targets in enumerate(target_lists)
        ]

    @property
    def items(self):
        items = self._items

        class Query:
            def exclude(self, **_):
                return self

            def order_by(self, *_):
                return items

        return Query()


class TargetPathTests(SimpleTestCase):
    def test_symbol_suffixes_are_taken_off(self):
        plan = Plan(
            ["src/carepath/api/routes_fhir.py:export_everything", "src/carepath/errors.py:X"],
            ["app/views.py::index", "app/models.py:42", "app/forms.py#L10"],
        )
        self.assertEqual(
            target_paths(plan),
            [
                "src/carepath/api/routes_fhir.py",
                "src/carepath/errors.py",
                "app/views.py",
                "app/models.py",
                "app/forms.py",
            ],
        )

    def test_the_same_file_named_twice_is_one_target(self):
        plan = Plan(["src/a.py:first"], ["src/a.py:second", "src/a.py"])
        self.assertEqual(target_paths(plan), ["src/a.py"])

    def test_a_cleaned_path_gets_a_test_path(self):
        paths = target_paths(Plan(["src/carepath/errors.py:ConsentWithheld"]))
        known = ["tests/test_consent.py", "tests/test_fhir.py"]
        self.assertEqual(
            derived_test_paths([{"path": path} for path in paths], known),
            ["tests/test_errors.py"],
        )
