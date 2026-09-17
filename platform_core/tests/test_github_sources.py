"""Resolving a GitHub link to the files worth importing.

The case that prompted these: a real, public repository with no README. A bare
repository link imported only the README, so the import died on GitHub's bare
"api.github.com returned HTTP 404" - which says nothing about what to do next,
and was wrong anyway, since the repository had importable files at its root.
"""

import json
import time
from unittest.mock import patch
from urllib.parse import urlparse

from django.test import SimpleTestCase, override_settings

from platform_core import github_sources
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
        # The .txt is documentation; the .esql is code and is left alone. The
        # name is the path within the repository rather than the repository
        # plus the path: the repository is the same for every file in an import,
        # so repeating it in each name only pushes the part that differs out of
        # sight.
        self.assertEqual(found, [("notes.txt", "https://raw.githubusercontent.com/o/r/main/notes.txt")])

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


@override_settings(IMPORT_MAX_FILES=100)
class DocumentSuffixTests(SimpleTestCase):
    """What a directory import will take, and what it still leaves alone.

    The GitHub importer used to accept text only, on the reasoning that anything
    else in a repository is code or binary. That holds for a repository at large
    and not for a documentation folder: a .docx there is documentation, the
    upload box on the same screen already accepted one, and SharePoint had been
    importing Office files all along. The same file was importable from one
    source and refused by the other.
    """

    def test_every_format_the_converter_handles_can_be_imported(self):
        from platform_core.fetching import CONTENT_SUFFIXES

        for suffix in set(CONTENT_SUFFIXES.values()):
            with self.subTest(suffix=suffix):
                self.assertIn(suffix, github_sources.DOC_SUFFIXES)

    def test_the_office_formats_a_documentation_set_is_written_in(self):
        for suffix in (".docx", ".xlsx", ".pptx", ".pdf", ".html"):
            self.assertIn(suffix, github_sources.DOC_SUFFIXES)

    def test_source_files_are_still_left_to_code_graph(self):
        for suffix in (".py", ".js", ".ts", ".go", ".java", ".sql", ".sh"):
            with self.subTest(suffix=suffix):
                self.assertNotIn(suffix, github_sources.DOC_SUFFIXES)

    def test_github_and_sharepoint_cannot_drift_apart(self):
        from platform_core import sharepoint

        self.assertEqual(github_sources.DOC_SUFFIXES, sharepoint.DOC_SUFFIXES)


@override_settings(IMPORT_MAX_FILES=3)
class TreeBoundTests(SimpleTestCase):
    """The walk is bounded, and says so when the bound bites."""

    def listing(self, count):
        return [
            {
                "type": "file",
                "name": f"doc{n}.md",
                "path": f"docs/doc{n}.md",
                "download_url": f"https://raw.example/{n}.md",
            }
            for n in range(count)
        ]

    def test_the_walk_stops_at_the_configured_ceiling(self):
        with patch.object(github_sources, "_api", return_value=self.listing(10)):
            found = github_sources.tree("o", "r", "main", "docs", "")
        self.assertEqual(len(found), 3)

    def test_reaching_the_ceiling_is_reported_with_the_numbers(self):
        notes = []
        with patch.object(github_sources, "_api", return_value=self.listing(10)):
            github_sources.tree("o", "r", "main", "docs", "", notes)
        self.assertEqual(len(notes), 1)
        self.assertIn("10 importable files", notes[0])
        self.assertIn("the limit is 3", notes[0])
        self.assertIn("7 were not", notes[0])

    def test_a_set_inside_the_ceiling_reports_nothing(self):
        notes = []
        with patch.object(github_sources, "_api", return_value=self.listing(2)):
            found = github_sources.tree("o", "r", "main", "docs", "", notes)
        self.assertEqual(len(found), 2)
        self.assertEqual(notes, [])

    @override_settings(IMPORT_MAX_FILES=50)
    def test_the_ceiling_follows_the_setting(self):
        with patch.object(github_sources, "_api", return_value=self.listing(40)):
            found = github_sources.tree("o", "r", "main", "docs", "")
        self.assertEqual(len(found), 40)


class RateLimitMessageTests(SimpleTestCase):
    """What a reader is told when GitHub refuses.

    GitHub reports an exhausted rate limit as 403, not 429, so the one thing a
    reader saw was "api.github.com returned HTTP 403" - which names neither the
    cause nor the wait, and reads as though the import is broken.
    """

    def quota(self, remaining, limit=60, in_seconds=1800):
        return api({
            "resources": {
                "core": {
                    "remaining": remaining,
                    "limit": limit,
                    "reset": int(time.time()) + in_seconds,
                }
            }
        })

    def test_an_exhausted_anonymous_quota_names_the_cause_and_the_wait(self):
        def responses(url, **kwargs):
            if url.endswith("/rate_limit"):
                return self.quota(0)
            raise FetchError("api.github.com returned HTTP 403.")

        with patch("platform_core.github_sources.fetch", side_effect=responses):
            with self.assertRaises(FetchError) as raised:
                plan(urlparse("https://github.com/o/r/tree/main/docs"))
        message = str(raised.exception)
        self.assertIn("rate limit is used up", message)
        self.assertIn("60 requests an hour", message)
        self.assertIn("about 29 minute(s)", message)
        self.assertIn("5,000 an hour", message)
        self.assertNotIn("HTTP 403", message)

    def test_a_credentialled_quota_does_not_suggest_mounting_one(self):
        def responses(url, **kwargs):
            if url.endswith("/rate_limit"):
                return self.quota(0, limit=5000)
            raise FetchError("api.github.com returned HTTP 403.")

        with patch("platform_core.github_sources.fetch", side_effect=responses):
            with self.assertRaises(FetchError) as raised:
                plan(urlparse("https://github.com/o/r/tree/main/docs"), "a-token")
        message = str(raised.exception)
        self.assertIn("5000 requests an hour for this credential", message)
        self.assertNotIn("Mounting a GitHub credential", message)

    def test_a_403_that_is_not_the_quota_says_something_else(self):
        """Quota intact means the refusal was about access, not about volume."""

        def responses(url, **kwargs):
            if url.endswith("/rate_limit"):
                return self.quota(59)
            raise FetchError("api.github.com returned HTTP 403.")

        with patch("platform_core.github_sources.fetch", side_effect=responses):
            with self.assertRaises(FetchError) as raised:
                plan(urlparse("https://github.com/o/r/tree/main/docs"))
        self.assertIn("GitHub refused the request", str(raised.exception))
        self.assertIn("mount a GitHub credential", str(raised.exception))

    def test_a_failed_diagnosis_does_not_replace_the_refusal_with_its_own_error(self):
        """The explanation is a courtesy; failing to get one must not mask the refusal."""

        def responses(url, **kwargs):
            raise FetchError("api.github.com returned HTTP 403.")

        with patch("platform_core.github_sources.fetch", side_effect=responses):
            with self.assertRaises(FetchError) as raised:
                plan(urlparse("https://github.com/o/r/tree/main/docs"))
        self.assertIn("GitHub refused the request", str(raised.exception))

    def test_other_failures_are_still_reported_as_they_were(self):
        with patch(
            "platform_core.github_sources.fetch",
            side_effect=FetchError("api.github.com returned HTTP 500."),
        ):
            with self.assertRaises(FetchError) as raised:
                plan(urlparse("https://github.com/o/r/tree/main/docs"))
        self.assertIn("HTTP 500", str(raised.exception))


class DocumentNamingTests(SimpleTestCase):
    """Names that read as the file, not as the path to it.

    A folder import named every document after the repository plus the whole
    path with the slashes turned into hyphens. Forty files then shared a
    thirty-character prefix and differed only at the end, which is unreadable in
    a list and identical once a graph label truncates it.
    """

    def test_the_directory_that_was_asked_for_is_not_repeated_in_every_name(self):
        self.assertEqual(
            github_sources.relative_name(
                "demo-artifacts/docs/07-govern/risk-register.xlsx", "demo-artifacts/docs"
            ),
            "07-govern/risk-register.xlsx",
        )

    def test_the_folder_below_it_is_kept_because_it_tells_files_apart(self):
        """Two README.md under different sections must not become one name."""
        first = github_sources.relative_name("docs/a/README.md", "docs")
        second = github_sources.relative_name("docs/b/README.md", "docs")
        self.assertNotEqual(first, second)
        self.assertEqual((first, second), ("a/README.md", "b/README.md"))

    def test_a_root_import_keeps_the_whole_path(self):
        self.assertEqual(github_sources.relative_name("docs/guide.md", ""), "docs/guide.md")

    def test_a_path_that_does_not_start_with_the_root_is_left_alone(self):
        self.assertEqual(github_sources.relative_name("other/guide.md", "docs"), "other/guide.md")

    def test_a_name_is_bounded_for_the_column_that_stores_it(self):
        self.assertLessEqual(len(github_sources.relative_name("x" * 400, "")), 200)

    @override_settings(IMPORT_MAX_FILES=100)
    def test_a_walk_names_its_files_relative_to_the_link(self):
        listing = [
            {
                "type": "file",
                "name": "risk-register.xlsx",
                "path": "demo-artifacts/docs/07-govern/risk-register.xlsx",
                "download_url": "https://raw.example/risk.xlsx",
            }
        ]
        with patch.object(github_sources, "_api", return_value=listing):
            found = github_sources.tree("o", "r", "main", "demo-artifacts/docs", "")
        self.assertEqual(found[0][0], "07-govern/risk-register.xlsx")
