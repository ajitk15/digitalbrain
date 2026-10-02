"""Read a repository from a folder on this server, for Code Graph.

A person types a path; the server reads it. That is a browser user choosing
which part of the server's disk to read, so it is offered only inside folders an
operator listed in `code_graph_local_roots`. Unset - the default - means the
option does not appear at all. Every check is repeated at index time, because
the operator can shorten the list after a folder was registered.

The folder is read, never run, and git is never executed against it: a
working tree's `.git/config` can name programs (`core.fsmonitor`, hooks) that
git would start. The commit is read from `.git/HEAD` and the ref files as plain
text instead. A folder that is not a git checkout is pinned by the digest of
what was read.

Nothing here fetches anything. `fetching.py` and `code_graph_clone.py` are
still the only two places this server reaches the network for content.
"""

import os
import re
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from django.conf import settings
from django.core.exceptions import ValidationError

from .code_graph_analysis import SKIP_DIRS, census
from .code_graph_clone import _setup_of, select_files

#: Entries a walk may visit before it stops. A working tree carries build
#: output and dependencies a clone never would, and the walk must terminate.
MAX_WALKED = 200_000
#: The most of any git metadata file read to find the commit.
GIT_READ_BYTES = 1_000_000
SHA = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")
REF = re.compile(r"refs/[A-Za-z0-9._/-]+")


def roots():
    return [Path(root).resolve() for root in settings.CODE_GRAPH_LOCAL_ROOTS]


def enabled():
    return bool(settings.CODE_GRAPH_LOCAL_ROOTS)


def _secret_directories():
    return [
        Path(directory).resolve()
        for directory in (
            settings.SECRET_DIRECTORY,
            settings.MANAGED_SECRET_DIRECTORY,
            settings.CONNECTOR_SECRET_DIRECTORY,
        )
        if directory
    ]


def resolve_folder(value):
    """The resolved folder, or a refusal. Never a path outside the roots."""
    allowed = roots()
    if not allowed:
        raise ValidationError("Reading folders on this server is not enabled.")
    text = (value or "").strip().strip('"')
    if not text or "\x00" in text:
        raise ValidationError("Enter the full path of a folder on this server.")
    candidate = Path(text)
    if not candidate.is_absolute():
        raise ValidationError("Enter the full path of a folder on this server.")
    try:
        folder = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValidationError(f"{text} does not exist on this server.") from None
    if not folder.is_dir():
        raise ValidationError(f"{text} is not a folder.")
    # Resolved first, so a symlink or junction inside a root that points out of
    # it is judged by where it lands, not by the name it was given.
    if not any(folder.is_relative_to(root) for root in allowed):
        raise ValidationError(
            "That folder is outside the folders this server may read: "
            + ", ".join(str(root) for root in allowed)
        )
    for secret in _secret_directories():
        if folder.is_relative_to(secret):
            raise ValidationError("That folder holds credentials and cannot be indexed.")
    if len(str(folder)) > 240:
        raise ValidationError("That path is too long to register.")
    return folder


def key(folder):
    """The unique identity of a folder: case-folded where the disk is."""
    return os.path.normcase(str(folder))


def path_of(repository):
    """The folder a local repository was registered with."""
    parsed = urlparse(repository.source_url)
    if parsed.scheme != "file":
        raise ValidationError("This repository was not registered from a folder.")
    # url2pathname decodes on Windows itself; decoding first as well would turn
    # a literal "%41" in a folder name into "A".
    return Path(url2pathname(parsed.path) if os.name == "nt" else unquote(parsed.path))


#: Subfolders one browse page lists. A folder of more than this is not one
#: anybody picks from by scrolling; typing the path still works.
MAX_LISTED = 500


def _skipped(name):
    return name in SKIP_DIRS or name.startswith(".") or name.startswith("cmake-build-")


def list_folders(folder):
    """([{name, path, repository}], more) for the subfolders worth offering.

    The walk's own rules: no symlinks or junctions, no skipped or hidden
    folders, nothing that holds credentials. `folder` must already have come
    through `resolve_folder`; this lists names and reads no file.
    """
    secrets = _secret_directories()
    try:
        entries = sorted(os.scandir(folder), key=lambda entry: entry.name.lower())
    except OSError:
        return [], False
    found = []
    for entry in entries:
        try:
            if entry.is_symlink() or entry.is_junction() or _skipped(entry.name):
                continue
            if not entry.is_dir(follow_symlinks=False):
                continue
            path = Path(entry.path)
            if any(path.resolve().is_relative_to(secret) for secret in secrets):
                continue
            if len(found) >= MAX_LISTED:
                return found, True
            found.append(
                {"name": entry.name, "path": str(path), "repository": (path / ".git").exists()}
            )
        except (OSError, ValueError):
            continue
    return found, False


def parent_of(folder):
    """The folder above, while that is still inside a root; else None."""
    if any(folder == root for root in roots()):
        return None
    try:
        return resolve_folder(str(folder.parent))
    except ValidationError:
        return None


def _read(path):
    try:
        if not path.is_file() or path.stat().st_size > GIT_READ_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _git_directory(folder):
    """The folder's git directory, if it has one inside the roots."""
    dot = folder / ".git"
    if dot.is_dir() and not dot.is_symlink():
        return dot
    # A worktree or submodule: `.git` is a file naming the real directory.
    # Followed only when that lands inside the roots too.
    text = _read(dot)
    if not text or not text.startswith("gitdir:"):
        return None
    target = Path(text.split(":", 1)[1].strip())
    if not target.is_absolute():
        target = folder / target
    try:
        target = target.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not target.is_dir() or not any(target.is_relative_to(root) for root in roots()):
        return None
    return target


def head_of(folder):
    """(commit, branch) the folder's checkout is on, read as text; ("", "") if none."""
    gitdir = _git_directory(folder)
    if gitdir is None:
        return "", ""
    common = gitdir
    pointer = _read(gitdir / "commondir")
    if pointer:
        candidate = Path(pointer.strip())
        candidate = (candidate if candidate.is_absolute() else gitdir / candidate).resolve()
        if candidate.is_dir() and any(candidate.is_relative_to(root) for root in roots()):
            common = candidate
    head = (_read(gitdir / "HEAD") or "").strip()
    if SHA.fullmatch(head):
        return head, ""
    if not head.startswith("ref:"):
        return "", ""
    ref = head.split(":", 1)[1].strip()
    if not REF.fullmatch(ref) or ".." in ref:
        return "", ""
    branch = ref.removeprefix("refs/heads/")
    for directory in (gitdir, common):
        value = (_read(directory / ref) or "").strip()
        if SHA.fullmatch(value):
            return value, branch
    for line in (_read(common / "packed-refs") or "").splitlines():
        parts = line.strip().split(" ", 1)
        if len(parts) == 2 and parts[1] == ref and SHA.fullmatch(parts[0]):
            return parts[0], branch
    # A branch with no commits yet.
    return "", branch


def _walk(folder):
    """(absolute, relative, size) for every file worth considering, and whether
    the walk finished. Symlinks and junctions are not followed, skipped and
    hidden directories are not entered, and nor is anything holding secrets."""
    secrets = _secret_directories()
    entries, walked, pending = [], 0, [folder]
    while pending:
        directory = pending.pop()
        try:
            listing = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in listing:
            walked += 1
            if walked > MAX_WALKED:
                return entries, False
            try:
                if entry.is_symlink() or entry.is_junction():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if _skipped(entry.name) or any(
                        Path(entry.path).resolve() == secret for secret in secrets
                    ):
                        continue
                    pending.append(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    absolute = Path(entry.path)
                    relative = absolute.relative_to(folder).as_posix()
                    entries.append((absolute, relative, entry.stat(follow_symlinks=False).st_size))
            except (OSError, ValueError):
                continue
    entries.sort(key=lambda item: item[1])
    return entries, True


def local_sources(folder, setup=None, languages=None):
    """Read one folder and return (commit, branch, files, warnings, complete).

    The same shape as `clone_sources`, so indexing does not care where the
    files came from. `commit` is "" for a folder that is not a git checkout;
    the caller pins that snapshot by its manifest digest instead.
    """
    commit, branch = head_of(folder)
    entries, finished = _walk(folder)
    warnings = []
    if not finished:
        warnings.append(
            f"The folder holds more than {MAX_WALKED} entries; the rest were not read."
        )
    if languages is not None:
        languages.extend(census((relative, size) for _, relative, size in entries))
    files, selected_warnings, complete = select_files(entries)
    warnings += selected_warnings
    complete = complete and finished
    if setup is not None:
        setup.update(_setup_of(folder))
    return commit, branch, files, warnings, complete
