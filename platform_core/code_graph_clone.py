"""Read a GitHub repository by cloning it, not by fetching one blob at a time.

The REST path spent one request per file against an anonymous budget of sixty an
hour, so any repository worth indexing exhausted the quota before it finished. A
clone is a single operation on a separate, far larger budget, and it resolves the
commit itself - this module makes no REST call at all.

`fetching.py` remains the only place a *user-supplied URL* is retrieved. Nothing
here accepts a URL. The caller supplies `owner/name`, already validated against
`code_graph_ingest.valid_name`, and the remote is built against a hardcoded
github.com, so no input of any kind chooses the host this talks to.

Repository content is read, never run:

* hooks are pointed at an empty directory, so nothing in the repository executes
* submodules are not followed - they are separate repositories with separate grants
* HOME, USERPROFILE and the git config files are redirected into a scratch
  directory, so a clone cannot read the operator's own git credentials, the same
  containment the Claude CLI sandbox applies for the same reason
* the checkout is deleted when indexing finishes
"""

import logging
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

from django.core.exceptions import ValidationError

from .code_graph_analysis import MAX_FILE_BYTES, MAX_FILES, MAX_TOTAL_BYTES, included

CLONE_TIMEOUT_SECONDS = 300
REMOTE = "https://github.com/{name}.git"
logger = logging.getLogger(__name__)


def _run(arguments, environment, cwd=None, timeout=CLONE_TIMEOUT_SECONDS):
    try:
        finished = subprocess.run(
            arguments,
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise ValidationError(
            "The repository took too long to download and was stopped."
        ) from None
    except OSError:
        raise ValidationError(
            "git could not be started on this server. Install git to index repositories."
        ) from None
    return finished


def _environment(scratch, token):
    """A git environment that can reach github.com and nothing of the operator's."""
    home = scratch / "home"
    hooks = scratch / "hooks"
    home.mkdir(exist_ok=True)
    hooks.mkdir(exist_ok=True)

    # The token goes in a config file rather than argv, where the process list
    # would expose it to every other user on the machine.
    config = scratch / "gitconfig"
    lines = ["[core]", f"\thooksPath = {hooks.as_posix()}"]
    if token:
        lines += [
            '[http "https://github.com/"]',
            f"\textraHeader = Authorization: Bearer {token}",
        ]
    config.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(config, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass

    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "SYSTEMROOT", "SystemRoot", "COMSPEC", "TEMP", "TMP", "LANG"}
    }
    environment.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "GIT_CONFIG_GLOBAL": str(config),
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            # Never sit waiting for a password on a server with no console.
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "",
            "GCM_INTERACTIVE": "never",
        }
    )
    return environment


def _remove(directory):
    """Delete the checkout, including the read-only files git leaves on Windows."""

    def force(action, name, _failure):
        try:
            os.chmod(name, stat.S_IWRITE)
            action(name)
        except OSError:
            logger.warning(
                "code_clone_cleanup_failed", extra={"event": "code_clone_cleanup_failed"}
            )

    shutil.rmtree(directory, onerror=force)


def _redact(message, token):
    text = (message or "").strip().splitlines()
    text = text[-1] if text else ""
    if token:
        text = text.replace(token, "***")
    return text[:200]


def clone_sources(name, ref, token):
    """Clone one repository and return (commit, branch, files, warnings, complete).

    `files` is a list of (path, text) with repository-relative POSIX paths, and
    the same inclusion, count and byte limits the analyser applies elsewhere.

    `branch` is the name the clone landed on. Asking git rather than GitHub
    costs nothing - the checkout is already here - and it is the answer for this
    repository rather than a guess at the convention it follows.
    """
    warnings = []
    complete = True
    with tempfile.TemporaryDirectory(prefix="code-graph-") as scratch_name:
        scratch = Path(scratch_name)
        checkout = scratch / "checkout"
        environment = _environment(scratch, token)

        arguments = [
            "git",
            "clone",
            "--depth",
            "1",
            "--single-branch",
            "--no-tags",
            "--no-recurse-submodules",
            "--quiet",
        ]
        if ref:
            arguments += ["--branch", ref]
        arguments += [REMOTE.format(name=name), str(checkout)]

        finished = _run(arguments, environment)
        if finished.returncode != 0:
            detail = _redact(finished.stderr, token)
            if ref and ("not found in upstream" in detail or "Remote branch" in detail):
                raise ValidationError(f"{name} has no branch or tag named {ref!r}.")
            if "Authentication failed" in detail or "could not read Username" in detail:
                raise ValidationError(
                    f"{name} refused the credential. Check that this application's "
                    "GitHub token can read it."
                )
            if "not found" in detail.lower() or "repository" in detail.lower():
                raise ValidationError(
                    f"{name} could not be cloned. If it is private, mount this "
                    f"application's GitHub credential. GitHub said: {detail}"
                )
            raise ValidationError(f"{name} could not be cloned. GitHub said: {detail}")

        head = _run(["git", "rev-parse", "HEAD"], environment, cwd=str(checkout), timeout=60)
        commit = (head.stdout or "").strip()
        if head.returncode != 0 or len(commit) != 40:
            raise ValidationError("The clone did not report a commit to pin the snapshot to.")

        # Which branch this is, so nothing has to guess later. A clone with no
        # --branch lands on the remote's default; one with --branch lands on
        # what was asked for. A tag is detached and reports the literal "HEAD",
        # which is not a branch name and must never be stored as one - that is
        # exactly what made the branch check ask GitHub for refs/heads/HEAD and
        # get nothing back.
        named = _run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            environment,
            cwd=str(checkout),
            timeout=60,
        )
        branch = (named.stdout or "").strip()
        if named.returncode != 0 or branch in ("", "HEAD"):
            branch = ref or ""

        files, total = [], 0
        for absolute in sorted(checkout.rglob("*")):
            if absolute.is_dir() or absolute.is_symlink():
                continue
            relative = absolute.relative_to(checkout).as_posix()
            if relative.startswith(".git/"):
                continue
            try:
                size = absolute.stat().st_size
            except OSError:
                continue
            if not included(relative, min(size, MAX_FILE_BYTES)):
                continue
            if len(files) >= MAX_FILES:
                complete = False
                warnings.append(f"Only the first {MAX_FILES} supported files were indexed.")
                break
            if total + size > MAX_TOTAL_BYTES:
                complete = False
                warnings.append(
                    "The source-size limit was reached; remaining files were not indexed."
                )
                break
            try:
                text = absolute.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                complete = False
                warnings.append(f"{relative} could not be read as UTF-8 text and was left out.")
                continue
            total += size
            files.append((relative, text))

        # Read everything out before the directory goes away; Windows keeps a
        # handle on a checkout until the last reader closes.
        _remove(checkout)
    return commit, branch, files, warnings, complete
