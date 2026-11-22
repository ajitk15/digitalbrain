"""Code Factory's checks by rule: a change that does not parse, or newly imports
what the repository does not have, is refused before anything is written.

Each refusal has a twin that must pass, because a check that refuses good
changes is worse than none: a person reruns it, it refuses again, and the
button is learned to be useless.
"""

from django.test import SimpleTestCase

from platform_core.code_factory_checks import (
    import_problems,
    syntax_problem,
    top_level_packages,
)

REPOSITORY = {
    "src/carepath/__init__.py": "",
    "src/carepath/services/__init__.py": "from .consent import record_consent\n",
    "src/carepath/services/consent.py": (
        "import logging\n"
        "LIMIT: int = 3\n"
        "log = logging.getLogger(__name__)\n"
        "class ConsentError(Exception):\n    pass\n"
        "def record_consent(patient):\n    return patient\n"
        "if True:\n    def fallback():\n        pass\n"
        "try:\n    from fast import speedups\nexcept ImportError:\n    speedups = None\n"
    ),
    "src/carepath/api/__init__.py": "",
    "src/carepath/api/routes.py": "from carepath.services.consent import record_consent\n",
    "src/carepath/dynamic.py": "def __getattr__(name):\n    return name\n",
    "tests/test_routes.py": "import pytest\n",
}


def texts(paths):
    return [(path, REPOSITORY[path]) for path in paths if path in REPOSITORY]


def problems(path, content, original=None, complete=True):
    return import_problems(
        [{"path": path, "content": content}],
        {path: original} if original else {},
        list(REPOSITORY),
        texts,
        complete=complete,
    )


class SyntaxTests(SimpleTestCase):
    def test_python_that_does_not_parse_is_refused_with_its_line(self):
        problem = syntax_problem("src/a.py", "def go(:\n    pass\n", "def go():\n    pass\n")
        self.assertIn("src/a.py: does not parse as Python (line 1", problem)

    def test_python_that_parses_passes(self):
        self.assertIsNone(syntax_problem("src/a.py", "def go():\n    return 1\n"))

    def test_a_new_file_is_judged_too(self):
        self.assertIsNotNone(syntax_problem("src/new.py", "return return"))

    def test_a_file_that_never_parsed_is_not_judged(self):
        """A Python 2 module, say: the rule cannot tell its syntax from ours."""
        self.assertIsNone(syntax_problem("legacy.py", "print 'b'\n", "print 'a'\n"))

    def test_json_and_toml_are_loaded(self):
        self.assertIn("JSON", syntax_problem("package.json", '{"a": }'))
        self.assertIn("TOML", syntax_problem("pyproject.toml", "[tool\nx = 1"))
        self.assertIsNone(syntax_problem("package.json", '{"a": 1}'))
        self.assertIsNone(syntax_problem("pyproject.toml", "[tool]\nx = 1\n"))

    def test_other_files_are_not_judged(self):
        self.assertIsNone(syntax_problem("app.js", "function ("))


class ImportTests(SimpleTestCase):
    def test_a_name_the_module_does_not_define_is_refused(self):
        """The live failure: tests written against a function nobody wrote."""
        found = problems(
            "tests/test_consent.py",
            "from carepath.services.consent import record_consent, withdraw_consent\n",
        )
        self.assertEqual(len(found), 1)
        self.assertIn("imports withdraw_consent from carepath.services.consent", found[0])
        self.assertIn("src/carepath/services/consent.py does not define", found[0])
        self.assertIn("line 1", found[0])

    def test_every_way_a_module_binds_a_name_counts(self):
        content = (
            "from carepath.services.consent import (\n"
            "    LIMIT, log, ConsentError, record_consent, fallback, speedups, logging\n)\n"
        )
        self.assertEqual(problems("tests/test_consent.py", content), [])

    def test_a_missing_module_of_this_repository_is_refused(self):
        found = problems("tests/test_audit.py", "from carepath.services.audit import write\n")
        self.assertEqual(len(found), 1)
        self.assertIn("imports carepath.services.audit, which is not in the repository", found[0])

    def test_a_missing_module_is_not_judged_when_the_index_was_partial(self):
        self.assertEqual(
            problems(
                "tests/test_audit.py",
                "from carepath.services.audit import write\n",
                complete=False,
            ),
            [],
        )

    def test_a_package_may_import_its_submodules_and_re_exports(self):
        self.assertEqual(
            problems(
                "tests/test_x.py",
                "from carepath.services import consent, record_consent\n"
                "import carepath.api.routes\n",
            ),
            [],
        )

    def test_relative_imports_resolve_from_the_file(self):
        self.assertEqual(
            problems(
                "src/carepath/api/routes.py",
                "from ..services.consent import record_consent\nfrom . import routes\n",
            ),
            [],
        )
        found = problems("src/carepath/api/routes.py", "from ..services.consent import nothing\n")
        self.assertIn("imports nothing from ..services.consent", found[0])

    def test_modules_outside_the_repository_are_not_judged(self):
        self.assertEqual(
            problems(
                "tests/test_x.py",
                "import json\nfrom django.db import models\nfrom pytest import nothing_here\n",
            ),
            [],
        )

    def test_an_import_already_in_the_original_is_not_the_changes_doing(self):
        original = "from carepath.services.consent import gone\n"
        self.assertEqual(
            problems("tests/test_x.py", original + "x = 1\n", original=original), []
        )

    def test_an_optional_import_is_left_alone(self):
        content = (
            "try:\n    from carepath.services.consent import later\n"
            "except ImportError:\n    later = None\n"
        )
        self.assertEqual(problems("tests/test_x.py", content), [])

    def test_a_module_that_can_hold_any_name_is_left_alone(self):
        self.assertEqual(problems("tests/test_x.py", "from carepath.dynamic import x\n"), [])

    def test_names_the_change_itself_adds_are_found(self):
        """A test for a function the same change writes: read as the change leaves it."""
        found = import_problems(
            [
                {
                    "path": "src/carepath/services/consent.py",
                    "content": "def withdraw_consent(patient):\n    return patient\n",
                },
                {
                    "path": "tests/test_consent.py",
                    "content": "from carepath.services.consent import withdraw_consent\n",
                },
                {"path": "src/carepath/services/audit.py", "content": "def write():\n    pass\n"},
                {
                    "path": "tests/test_audit.py",
                    "content": "from carepath.services.audit import write\n",
                },
            ],
            {},
            list(REPOSITORY),
            texts,
        )
        self.assertEqual(found, [])

    def test_a_file_that_does_not_parse_is_left_to_the_syntax_check(self):
        self.assertEqual(problems("tests/test_x.py", "from carepath import (\n"), [])

    def test_a_package_name_at_two_roots_is_not_judged(self):
        paths = ["a/pkg/__init__.py", "b/pkg/__init__.py", "pkg_only/__init__.py"]
        packages = top_level_packages(paths)
        self.assertEqual(packages["pkg"], {"a", "b"})
        self.assertEqual(packages["pkg_only"], {""})
        found = import_problems(
            [{"path": "t.py", "content": "from pkg import nothing\n"}], {}, paths, lambda _: []
        )
        self.assertEqual(found, [])
