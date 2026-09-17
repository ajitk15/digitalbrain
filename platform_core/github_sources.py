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
import time

from .fetching import DOCUMENT_SUFFIXES, FetchError, fetch

API = "https://api.github.com"

#: Extensions worth importing from a documentation tree - everything the offline
#: converter handles, shared with SharePoint so the two cannot drift apart again.
#: Source files are still left alone: they belong in Code Graph, not in knowledge.
DOC_SUFFIXES = DOCUMENT_SUFFIXES

#: A tree walk must terminate, and a knowledge base is not a mirror of a repo.
#: The width is configurable, the depth is not: depth is what makes the walk
#: terminate at all, and no documentation set is nested four folders deep.
MAX_DEPTH = 4


def max_files():
    """The configured ceiling, read per call so a test can override the setting."""
    from django.conf import settings

    return settings.IMPORT_MAX_FILES

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


def quota_message(token):
    """Why GitHub refused, when the refusal was a rate limit.

    Asked only on the error path, and `/rate_limit` is itself exempt from the
    quota, so this costs nothing that was going to work anyway. Returning None
    means the refusal was not about the quota and the caller should say
    something else.
    """
    try:
        body, _, _, _ = fetch(f"{API}/rate_limit", headers=headers(token))
        core = json.loads(body)["resources"]["core"]
        remaining, limit, reset = core["remaining"], core["limit"], core["reset"]
    except Exception:
        # The diagnosis is a courtesy. Failing to obtain it must not replace the
        # original refusal with an error about the diagnosis.
        return None
    if remaining > 0:
        return None
    minutes = max(0, int((reset - time.time()) // 60))
    when = "in under a minute" if minutes < 1 else f"in about {minutes} minute(s)"
    if token:
        return (
            f"GitHub's rate limit is used up: {limit} requests an hour for this credential. "
            f"It resets {when}."
        )
    return (
        f"GitHub's rate limit is used up: {limit} requests an hour for anonymous access, and "
        f"one import spends one request per folder. It resets {when}. Mounting a GitHub "
        "credential for this application raises the limit to 5,000 an hour."
    )


def _api(url, token, *, missing=None):
    """Call the contents API.

    `missing` replaces GitHub's bare "returned HTTP 404" with something the
    reader can act on. GitHub answers 404 both for "no such thing" and for
    "private, and you sent no credential", so the wording has to cover both
    without asserting which - it cannot tell them apart either.

    A 403 gets the same treatment. GitHub reports an exhausted rate limit as 403
    rather than 429, so "api.github.com returned HTTP 403" was the one thing a
    reader saw, and it names neither the cause nor the wait.
    """
    try:
        body, _, _, _ = fetch(url, headers=headers(token))
    except FetchError as failure:
        if missing and "HTTP 404" in str(failure):
            raise FetchError(missing) from None
        if "HTTP 403" in str(failure) or "HTTP 429" in str(failure):
            explained = quota_message(token)
            if explained:
                raise FetchError(explained) from None
            raise FetchError(
                "GitHub refused the request. If the repository is private, mount a GitHub "
                "credential for this application."
            ) from None
        raise
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
    """The repository README, or None when the repository has none.

    Absent is not an error here: plenty of repositories have no README, and a
    bare repository link should still import what documentation the repository
    does have rather than refusing outright.
    """
    try:
        data = _api(f"{API}/repos/{owner}/{repo}/readme", token)
    except FetchError as failure:
        if "HTTP 404" in str(failure):
            return None
        raise
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


def relative_name(entry_path, root):
    """A document name that reads as the file it is, not as where it lives.

    `root` is the directory the link named, so its prefix carries no
    information: everything in the import shares it. What is left is the path
    below it, which is both short and unique within one import.
    """
    prefix = f"{root.strip('/')}/" if root else ""
    relative = entry_path[len(prefix):] if prefix and entry_path.startswith(prefix) else entry_path
    return (relative or entry_path)[:200]


def tree(owner, repo, ref, path, token, notes=None):
    """Every documentation file under a directory, breadth-first and bounded.

    `notes` collects anything the person should be told about the result. The
    only note today is that the ceiling was reached: a walk that quietly returns
    the first N of M files leaves someone believing their documentation set is
    complete when part of it was never queued.
    """
    ceiling = max_files()
    found = []
    skipped = 0
    queue = [(path, 0)]
    while queue:
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
                if not name.lower().endswith(DOC_SUFFIXES):
                    continue
                if len(found) >= ceiling:
                    # Counted rather than abandoned, so the message can say how
                    # many were left behind instead of only that some were.
                    skipped += 1
                    continue
                # Named relative to the directory that was asked for, with the
                # separators kept. The old name was the repository plus the
                # whole path with every slash turned into a hyphen, so a folder
                # import produced forty names sharing a thirty-character prefix
                # and differing only at the end - unreadable in a list, and
                # identical once a graph label truncated them.
                found.append((relative_name(entry.get("path") or name, path),
                              _download_url(entry)))
    if skipped and notes is not None:
        notes.append(
            f"This link holds {len(found) + skipped} importable files and the limit is "
            f"{ceiling}. The first {ceiling} were queued; {skipped} were not. Import a "
            "subfolder to bring in the rest, or raise import_max_files."
        )
    if not found:
        raise FetchError(
            "No documentation files were found there. This imports "
            + ", ".join(DOC_SUFFIXES)
            + " files."
        )
    return found


def plan(parsed, token="", notes=None):
    """The files a GitHub link resolves to, as a list of (name, download url)."""
    kind, owner, repo, ref, path = parse(parsed)
    if kind == "repo":
        found = readme(owner, repo, token)
        if found:
            return found
        # No README. Take the documentation at the repository root instead, which
        # is what the person pasting the link wanted; failing here would leave a
        # public repository unimportable for want of one file.
        try:
            return tree(owner, repo, "", "", token, notes)
        except FetchError:
            # GitHub answers 404 for private and for absent alike, so ask whether
            # the repository is visible at all before blaming its contents.
            if not repository_exists(owner, repo, token):
                raise FetchError(
                    f"{owner}/{repo} could not be read. Check the name, or mount a GitHub "
                    "credential for this application if the repository is private."
                ) from None
            raise FetchError(
                f"{owner}/{repo} has no README and nothing importable at its root. "
                "This imports " + ", ".join(DOC_SUFFIXES) + " files; link a /tree/ "
                "directory or a /blob/ file to bring in something else."
            ) from None
    if kind == "blob":
        return single_file(owner, repo, ref, path, token)
    return tree(owner, repo, ref, path, token, notes)


def repository_exists(owner, repo, token):
    """Whether the repository is visible to us at all, for a clearer message."""
    try:
        _api(f"{API}/repos/{owner}/{repo}", token)
    except FetchError:
        return False
    return True
