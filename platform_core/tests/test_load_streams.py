"""The load harness's own arithmetic (`manage.py load_streams`).

The harness itself runs servers and a client process for minutes, so it is a
measuring tool, not a test; what it reports is checked here.
"""

from django.test import SimpleTestCase

from platform_core import load_clients
from platform_core.load_clients import Stage, percentile


class PercentileTests(SimpleTestCase):
    def test_nearest_rank(self):
        values = list(range(1, 101))
        self.assertEqual(percentile(values, 50), 50)
        self.assertEqual(percentile(values, 95), 95)
        self.assertEqual(percentile([7], 95), 7)
        self.assertIsNone(percentile([], 95))


class StageTests(SimpleTestCase):
    def test_refusals_are_summed_across_statuses(self):
        stage = Stage(server="uvicorn", readers=4, answers=2, refused={"503": 2, "404": 1})
        self.assertEqual(stage.summary()["refused"], 3)

    def test_the_client_process_imports_nothing_from_django(self):
        """Its parsing must not share the measured server's interpreter, or its settings."""
        import ast
        from pathlib import Path

        tree = ast.parse(Path(load_clients.__file__).read_text(encoding="utf-8"))
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree) if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            (node.module or "").split(".")[0]
            for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        }
        self.assertFalse(imported & {"django", "platform_core", "digitalbrain"}, imported)
