"""Existing tests that cover a changed file, shown to the implementation and the
test author.

A live run changed an endpoint without being shown its existing tests. CI then
failed on two: one the ticket had made wrong (an export without consent was
expected to succeed), and one the change had broken (an unknown patient now got
403 instead of 404). Neither agent could have known either.
"""

import json
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings

from platform_core.code_factory import (
    NEW_FILE,
    covering_tests,
    numbered_files,
    orphaned_tests,
    prepare,
)
from platform_core.code_graph_ingest import register
from platform_core.models import FactoryRun, PlanItem, ProposedChange

from . import test_code_factory_delivery as delivery

ROUTE = "from fastapi import APIRouter\n@router.get('/Patient/{patient_id}/$everything')\n"
RAW = "/Patient/{patient_id}/$everything"
TEST_FHIR = "def test_export(client):\n    client.get('/fhir/Patient/1/$everything')\n"


@override_settings(**delivery.SETTINGS)
class CoveringTestTests(TestCase):
    def setUp(self):
        delivery.DeliveryGateTests.setUp(self)
        PlanItem.objects.filter(plan=self.plan).update(
            targets=["src/api/routes_fhir.py"], change_summary="Check consent first."
        )
        self.snapshot = register(self.owner, self.app.pk, "acme/widgets").snapshots.create(
            number=1, commit_sha="c" * 40
        )
        for path, text, routes in (
            (
                "src/api/routes_fhir.py",
                ROUTE,
                [{"method": "GET", "path": "/Patient/*/everything", "raw": RAW}],
            ),
            ("tests/test_fhir.py", TEST_FHIR, []),
            ("tests/test_patients.py", "def test_patient(): 'Patient'\n", []),
            ("tests/test_health.py", "def test_ok(): 'Patient'\n", []),
        ):
            self.snapshot.files.create(
                path=path, language="python", digest="d", content=text, routes=routes
            )
        FactoryRun.objects.filter(pk=self.run.pk).update(code_snapshot=self.snapshot)
        self.run.refresh_from_db()

    def test_the_test_reaching_the_route_is_found_and_common_words_are_not(self):
        self.assertEqual(
            covering_tests(self.snapshot, ["src/api/routes_fhir.py"]),
            {"tests/test_fhir.py": ["src/api/routes_fhir.py"]},
        )

    def test_the_test_author_may_update_a_covering_test(self):
        current = {
            "src/api/routes_fhir.py": {
                "path": "src/api/routes_fhir.py",
                "text": ROUTE,
                "sha": "s1",
            },
            "tests/test_fhir.py": {"path": "tests/test_fhir.py", "text": TEST_FHIR, "sha": "s2"},
        }
        order = json.dumps({"files": [{"file": "1", "intent": "Check consent", "checks": []}]})
        implementation = json.dumps({"files": [{"file": "1", "content": ROUTE + "# consent\n"}]})
        updated = json.dumps(
            {"files": [{"file": "1", "content": "def test_export(client, consented): ...\n"}]}
        )
        verdict = json.dumps(
            {"files": [{"file": "1", "verdict": "ok"}, {"file": "2", "verdict": "ok"}]}
        )
        with patch("platform_core.code_factory.write_credential", return_value="tok"):
            with patch(
                "platform_core.github_write.read_file",
                side_effect=lambda repo, path, ref, token: current.get(path),
            ):
                with patch("platform_core.github_write.list_directory", return_value=[]):
                    with patch(
                        "platform_core.ai.invoke_ai",
                        side_effect=[order, implementation, updated, verdict],
                    ) as model:
                        prepare(self.owner, self.app.pk, self.run.pk)
        # The implementation saw the existing test, labelled as one.
        self.assertIn("--- tests/test_fhir.py (EXISTING TEST", model.call_args_list[1].args[3])
        # The test author was handed it to update, marked as an existing test.
        shown = model.call_args_list[2].args[4]
        self.assertTrue(shown[0]["excerpt"].startswith("EXISTING TEST covering"))
        self.assertEqual(
            sorted(ProposedChange.objects.filter(run=self.run).values_list("path", flat=True)),
            ["src/api/routes_fhir.py", "tests/test_fhir.py"],
        )


class NumberedFilesTests(SimpleTestCase):
    def test_a_snapshot_file_shows_its_contents_not_new_file(self):
        """Snapshot reads carry no blob sha; they were shown as NEW FILE."""
        entry = numbered_files([{"path": "src/a.py", "text": "x = 1\n", "sha": None}])[0]
        self.assertEqual(entry["excerpt"], "x = 1\n")
        absent = numbered_files([{"path": "src/b.py", "text": "", "sha": None}])[0]
        self.assertEqual(absent["excerpt"], NEW_FILE)


class CoveredOrphanTests(SimpleTestCase):
    def test_an_update_goes_when_the_code_it_covers_is_rejected(self):
        kept = [
            {"path": "src/other.py", "test_for_changed_code": False},
            {
                "path": "tests/test_fhir.py",
                "test_for_changed_code": True,
                "covers": ["src/api/routes_fhir.py"],
            },
        ]
        orphaned = orphaned_tests(kept, ["src/api/routes_fhir.py"])
        self.assertEqual([path for path, _ in orphaned], ["tests/test_fhir.py"])
