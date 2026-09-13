"""Resolving a GitHub link to the files worth importing.

The case that prompted these: a real, public repository with no README. A bare
repository link imported only the README, so the import died on GitHub's bare
"api.github.com returned HTTP 404" - which says nothing about what to do next,
and was wrong anyway, since the repository had importable files at its root.
"""

import json
from unittest.mock import patch
from urllib.parse import urlparse

from django.test import SimpleTestCase

from platform_core.fetching import FetchError
from platform_core.github_sources import DOC_SUFFIXES, parse, plan


def api(payload):
    return json.dumps(payload).encode(), "application/json", "", ""


class ParseTests(SimpleTestCase):
    def test_a_clone_url_is_understood(self):
        """`.git` is what GitHub's own clone button hands you."""
        self.assertEqual(
            parse(urlparse("https://github.com/owner/repo.git")),
            ("repo", "owner", "repo", "", ""),
        )

    def test_repo_file_and_directory_links_are_distinguished(self):
        self.assertEqual(parse(urlparse("https://github.com/o/r"))[0], "repo")
        self.assertEqual(parse(urlparse("https://github.com/o/r/blob/main/a.md"))[0], "blob")
        self.assertEqual(parse(urlparse("https://github.com/o/r/tree/main/docs"))[0], "tree")

    def test_a_link_that_is_not_a_repository_is_refused_clearly(self):
        with self.assertRaises(FetchError) as raised:
            parse(urlparse("https://github.com/owner"))
        self.assertIn("owner/repo", str(raised.exception))


class RepositoryWithoutReadmeTests(SimpleTestCase):
    """A missing README falls back to the repository root."""

    def responses(self, *bodies):
        return patch("platform_core.github_sources.fetch", side_effect=list(bodies))

    def not_found(self):
        return FetchError("api.github.com returned HTTP 404.")

    def test_a_repository_with_no_readme_imports_its_root_documentation(self):
        root = [
            {"type": "file", "name": "notes.txt", "path": "notes.txt",
             "download_url": "https://raw.githubusercontent.com/o/r/main/notes.txt"},
            {"type": "file", "name": "build.esql", "path": "build.esql",
             "download_url": "https://raw.githubusercontent.com/o/r/main/build.esql"},
        ]
        with self.responses(self.not_found(), api(root)):
            found = plan(urlparse("https://github.com/o/r"))
        # The .txt is documentation; the .esql is code and is left alone.
        self.assertEqual(found, [("r-notes.txt", "https://raw.githubusercontent.com/o/r/main/notes.txt")])

    def test_a_readme_is_still_preferred_when_there_is_one(self):
        readme = {"name": "README.md", "download_url": "https://raw.githubusercontent.com/o/r/main/README.md"}
        with self.responses(api(readme)) as fetch:
            found = plan(urlparse("https://github.com/o/r"))
        self.assertEqual(found[0][0], "r-README.md")
        self.assertEqual(fetch.call_count, 1)

    def test_a_repository_with_nothing_importable_says_what_it_accepts(self):
        with self.responses(self.not_found(), api([]), api({"name": "r"})):
            with self.assertRaises(FetchError) as raised:
                plan(urlparse("https://github.com/o/r"))
        message = str(raised.exception)
        self.assertIn("no README and nothing importable", message)
        self.assertIn(DOC_SUFFIXES[0], message)
        self.assertNotIn("HTTP 404", message)

    def test_a_private_or_missing_repository_names_the_credential_to_mount(self):
        """GitHub answers 404 for private and absent alike, so cover both."""
        with self.responses(self.not_found(), self.not_found(), self.not_found()):
            with self.assertRaises(FetchError) as raised:
                plan(urlparse("https://github.com/o/r"))
        message = str(raised.exception)
        self.assertIn("could not be read", message)
        self.assertIn("private", message)
        self.assertNotIn("HTTP 404", message)

    def test_a_failure_that_is_not_a_404_is_not_swallowed(self):
        with self.responses(FetchError("api.github.com returned HTTP 500.")):
            with self.assertRaises(FetchError) as raised:
                plan(urlparse("https://github.com/o/r"))
        self.assertIn("500", str(raised.exception))
