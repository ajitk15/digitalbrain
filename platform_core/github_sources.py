"""Turning a GitHub link into the set of files worth importing.

Three shapes are recognised, so one paste box can serve all of them:

* ``github.com/owner/repo``                -> the repository README
* ``github.com/owner/repo/blob/ref/path``  -> that one file
* ``github.com/owner/repo/tree/ref/dir``   -> every documentation file in that
                                              directory, recursively

Only the contents API is used, and only for reading. Nothing here writes to a
repository, and no request is made without going through the hardened fetcher in
`fetching`, so GitHub is subject to the same address, size and redirect rules as
any other host.

A per-application ``github_<id>`` secret is used when one is mounted, which is what
makes private repositories work; without it, public repositories still import and
the shared unauthenticated rate limit applies.
"""

import json
import re

from .fetching import FetchError, fetch

API = "https://api.github.com"

#: Extensions worth importing from a documentation tree. Everything else in a repo
#: is code or binary, which belongs in Code Factory rather than in knowledge.
DOC_SUFFIXES = (".md", ".markdown", ".rst", ".txt", ".adoc", ".csv", ".json", ".yaml", ".yml")

#: A tree walk must terminate, and a knowledge base is not a mirror of a repo.
MAX_FILES = 25
MAX_DEPTH = 4

REPO = r"[A-Za-z0-9_.-]+"
OWNER = r"[A-Za-z0-9-]+"


def is_github(parsed):
    host = (parsed.hostname or "").lower()
    return host in {"github.com", "www.github.com", "raw.githubusercontent.com"}


def parse(parsed):
    """Classify a GitHub URL.

    Returns (kind, owner, repo, ref, path) where kind is repo, blob or tree.
    Raises FetchError when the link is not one this importer understands.
    """
    host = (parsed.hostname or "").lower()
    parts = [p for p in (parsed.path or "").split("/") if p]

    if host == "raw.githubusercontent.com":
        if len(parts) < 4:
            raise FetchError("That raw GitHub link is missing a file path.")
        return "blob", parts[0], parts[1], parts[2], "/".join(parts[3:])

    if len(parts) < 2:
        raise FetchError("Point at a repository, for example github.com/owner/repo.")
    owner, repo = parts[0], parts[1]
    if not re.fullmatch(OWNER, owner) or not re.fullmatch(REPO, repo) or repo in {".", ".."}:
        raise FetchError("That does not look like a GitHub repository.")
    repo = repo[:-4] if repo.endswith(".git") else repo

    if len(parts) == 2:
        return "repo", owner, repo, "", ""
    kind = parts[2]
    if kind not in {"blob", "tree"}:
        raise FetchError(
            "Link to a repository, a file (/blob/) or a directory (/tree/) to import it."
        )
    if len(parts) < 4:
        raise FetchError("That link is missing a branch or tag.")
    return kind, owner, repo, parts[3], "/".join(parts[4:])


def headers(token):
    value = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        value["Authorization"] = f"Bearer {token}"
    return value


def _api(url, token):
    body, _, _, _ = fetch(url, headers=headers(token))
    try:
        return json.loads(body)
    except ValueError:
        raise FetchError("GitHub returned an unexpected response.") from None


def _download_url(entry):
    url = entry.get("download_url")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise FetchError("GitHub did not provide a download link for that file.")
    return url


def readme(owner, repo, token):
    """The repository README as a single (name, url) pair."""
    data = _api(f"{API}/repos/{owner}/{repo}/readme", token)
    if not isinstance(data, dict):
        raise FetchError("GitHub returned an unexpected response.")
    return [(f"{repo}-{data.get('name') or 'README.md'}", _download_url(data))]


def single_file(owner, repo, ref, path, token):
    if not path:
        raise FetchError("That link does not name a file.")
    data = _api(f"{API}/repos/{owner}/{repo}/contents/{path}?ref={ref}", token)
    if isinstance(data, list):
        raise FetchError("That link points at a directory. Use a /tree/ link to import it.")
    if not isinstance(data, dict) or data.get("type") != "file":
        raise FetchError("That link does not name a file.")
    return [(f"{repo}-{data.get('name') or path.rsplit('/', 1)[-1]}", _download_url(data))]


def tree(owner, repo, ref, path, token):
    """Every documentation file under a directory, breadth-first and bounded."""
    found = []
    queue = [(path, 0)]
    while queue and len(found) < MAX_FILES:
        current, depth = queue.pop(0)
        suffix = f"?ref={ref}" if ref else ""
        entries = _api(f"{API}/repos/{owner}/{repo}/contents/{current}{suffix}", token)
        if isinstance(entries, dict):
            entries = [entries]
        if not isinstance(entries, list):
            raise FetchError("GitHub returned an unexpected directory listing.")
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if entry.get("type") == "dir" and depth < MAX_DEPTH:
                queue.append((entry.get("path", ""), depth + 1))
            elif entry.get("type") == "file":
                name = entry.get("name") or ""
                if name.lower().endswith(DOC_SUFFIXES) and len(found) < MAX_FILES:
                    label = (entry.get("path") or name).replace("/", "-")
                    found.append((f"{repo}-{label}", _download_url(entry)))
    if not found:
        raise FetchError(
            "No documentation files were found there. This imports "
            + ", ".join(DOC_SUFFIXES)
            + " files."
        )
    return found


def plan(parsed, token=""):
    """The files a GitHub link resolves to, as a list of (name, download url)."""
    kind, owner, repo, ref, path = parse(parsed)
    if kind == "repo":
        return readme(owner, repo, token)
    if kind == "blob":
        return single_file(owner, repo, ref, path, token)
    return tree(owner, repo, ref, path, token)
