"""The code a change depends on, shown to the implementation read-only.

A live run's design named the export route but not the consent service the fix
had to call, and the implementation - shown only the named files - declined to
invent how consent is read, twice. It is now also shown the modules the approved
gaps name by word and what its targets import, as reference it cannot edit.
"""

import json
from unittest.mock import patch

from django.test import TestCase, override_settings

from platform_core.code_factory import prepare, reference_files
from platform_core.code_graph_ingest import register
from platform_core.models import CodeRelationship, FactoryRun, PlanItem

from . import test_code_factory_delivery as delivery


@override_settings(**delivery.SETTINGS)
class ReferenceFileTests(TestCase):
    def setUp(self):
        delivery.DeliveryGateTests.setUp(self)
        PlanItem.objects.filter(plan=self.plan).update(
            change_summary="Wait with the backoff module before each retry."
        )
        repository = register(self.owner, self.app.pk, "acme/widgets")
        self.snapshot = repository.snapshots.create(number=1, commit_sha="c" * 40)
        files = {
            path: self.snapshot.files.create(path=path, language="python", digest="d", content=text)
            for path, text in {
                "src/queue.py": "from src import clock\n",
                "src/backoff.py": "def wait(attempt):\n    return 2 ** attempt\n",
                "src/clock.py": "def now():\n    return 0\n",
                "src/config.py": "DEBUG = False\n",
                "tests/test_backoff.py": "def test_wait(): pass\n",
            }.items()
        }
        CodeRelationship.objects.create(
            snapshot=self.snapshot,
            source=files["src/queue.py"],
            target=files["src/clock.py"],
            confidence="static",
        )
        FactoryRun.objects.filter(pk=self.run.pk).update(code_snapshot=self.snapshot)
        self.run.refresh_from_db()

    def test_named_modules_and_imports_are_chosen_and_nothing_else(self):
        chosen = [item["path"] for item in reference_files(self.run, [{"path": "src/queue.py"}])]
        # "backoff" is named by the gap; clock is imported by the target.
        self.assertEqual(chosen, ["src/backoff.py", "src/clock.py"])
        # Never the target itself, a test file, or a generic module.
        self.assertNotIn("src/queue.py", chosen)
        self.assertNotIn("tests/test_backoff.py", chosen)
        self.assertNotIn("src/config.py", chosen)

    def test_imports_are_followed_two_levels_deep(self):
        """What decides a request's behaviour is often one module further away.

        A live run's route reached the database only through its dependency
        module, never saw that an error rolls the transaction back, and wrote an
        audit record the rollback discarded.
        """
        clock = self.snapshot.files.get(path="src/clock.py")
        db = self.snapshot.files.create(
            path="src/db.py", language="python", digest="d", content="def transaction(): ...\n"
        )
        CodeRelationship.objects.create(
            snapshot=self.snapshot, source=clock, target=db, confidence="static"
        )
        chosen = [item["path"] for item in reference_files(self.run, [{"path": "src/queue.py"}])]
        self.assertEqual(chosen, ["src/backoff.py", "src/clock.py", "src/db.py"])

    def test_the_implementation_sees_them_read_only_and_cannot_edit_them(self):
        order = json.dumps({"files": [{"file": "1", "intent": "Use backoff", "checks": []}]})
        # The model tries to edit the reference file as "file 2": it is not a
        # numbered file, so that entry is simply not accepted.
        implementation = json.dumps(
            {
                "files": [
                    {"file": "1", "content": "from src import backoff\n"},
                    {"file": "2", "content": "hijacked"},
                ]
            }
        )
        verdict = json.dumps({"files": [{"file": "1", "verdict": "ok", "reason": ""}]})
        current = {"path": "src/queue.py", "text": "from src import clock\n", "sha": "s1"}
        with patch("platform_core.code_factory.write_credential", return_value="tok"):
            with patch("platform_core.github_write.read_file", return_value=current):
                with patch(
                    "platform_core.ai.invoke_ai", side_effect=[order, implementation, verdict]
                ) as model:
                    prepare(self.owner, self.app.pk, self.run.pk)
        question = model.call_args_list[1].args[3]
        self.assertIn("READ-ONLY REFERENCE from snapshot v1", question)
        self.assertIn("--- src/backoff.py ---", question)
        self.assertIn("def wait(attempt)", question)
        from platform_core.models import ProposedChange

        self.assertEqual(
            list(ProposedChange.objects.filter(run=self.run).values_list("path", flat=True)),
            ["src/queue.py"],
        )
        output = self.run.phases.get(name="implementation").output
        self.assertEqual(output["references"], ["src/backoff.py", "src/clock.py"])
        # The review is shown the same reference, so it can check calls against
        # real code. It used to see none of it.
        review_question = model.call_args_list[2].args[3]
        self.assertIn("--- src/backoff.py ---", review_question)

    def test_test_fixtures_are_part_of_the_reference(self):
        """A test author not shown conftest.py invents fixtures."""
        from platform_core.code_factory import test_support

        self.snapshot.files.create(
            path="tests/conftest.py", language="python", digest="d", content="def client(): ...\n"
        )
        self.assertEqual(
            test_support(self.snapshot, ["tests/test_backoff.py"]), ["tests/conftest.py"]
        )
        chosen = reference_files(self.run, [{"path": "src/queue.py"}])
        support = [item["path"] for item in chosen if item["support"]]
        self.assertEqual(support, ["tests/conftest.py"])
