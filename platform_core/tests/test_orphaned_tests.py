"""A test file never goes out without the code it was written for.

A live run's reviewer rejected the one source file, and the change that was left
was the test for it - checking a consent guard that was never written. These pin
that such a test goes with its subject, and that a test the plan named stays.
"""

from django.test import SimpleTestCase

from platform_core.code_factory import orphaned_tests, test_subject


def change(path, chosen=False):
    return {"path": path, "content": "x", "sha": None, "test_for_changed_code": chosen}


class OrphanedTestTests(SimpleTestCase):
    def test_the_subject_is_read_from_the_test_name(self):
        self.assertEqual(test_subject("tests/test_routes_fhir.py"), "routes_fhir")
        self.assertEqual(test_subject("web/cart.test.ts"), "cart")

    def test_a_test_goes_with_its_rejected_subject(self):
        kept = [change("src/a.py"), change("tests/test_routes_fhir.py", chosen=True)]
        orphaned = orphaned_tests(kept, ["src/carepath/api/routes_fhir.py"])
        self.assertEqual([path for path, _ in orphaned], ["tests/test_routes_fhir.py"])

    def test_a_test_goes_when_no_source_is_left(self):
        kept = [change("tests/test_other.py", chosen=True)]
        self.assertEqual(len(orphaned_tests(kept, ["src/elsewhere.py"])), 1)

    def test_a_test_the_plan_named_stays(self):
        kept = [change("tests/test_routes_fhir.py", chosen=False)]
        self.assertEqual(orphaned_tests(kept, ["src/carepath/api/routes_fhir.py"]), [])

    def test_a_test_for_code_that_survived_stays(self):
        kept = [change("src/queue.py"), change("tests/test_queue.py", chosen=True)]
        self.assertEqual(orphaned_tests(kept, ["src/other.py"]), [])
