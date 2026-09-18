"""Opening a pull request, through the GitHub API rather than a git checkout.

This is the only place the platform writes anywhere outside its own database, so
the boundaries are worth stating plainly.

**No git, no clone, no subprocess.** Everything is HTTPS JSON against a fixed
host, api.github.com, through `fetching.fetch` - so resolution, the
private-address refusal, the pinned connection, the no-redirect rule and the size
caps all apply. There is no working directory to escape from and no command line
to inject into.

**A separate credential.** Reading a repository uses ``github_<app>``; writing
uses ``github_write_<app>``. They are different files, so granting an application
the ability to import issues does not grant it the ability to push. An
application with no write credential mounted simply cannot deliver, and says so.

**Only paths a person approved.** Paths come from a plan a second person
approved, never from the model that writes the contents -- that one addresses
files by the number it was shown them with and cannot name a path at all. A
change either replaces a file the platform first read at the branch it is about
to write to, or creates one the plan named that is not there.

**Creation is explicit at every step.** A path counts as new only when GitHub
answers 404, which is why `absent_path` exists rather than reading "read_file
returned None" as "not there" -- that also covers a file too large to read, a
binary, and a directory, and creating over any of those would be a silent
overwrite. Verification re-checks the path is still absent, and the commit
carries no blob sha, so GitHub refuses it if something appeared in between.
Nothing is deleted, and a path that fails `safe_path` is refused rather than
sanitised.
"""

import base64
import json

from django.core.exceptions import ValidationError

from .fetching import FetchError, fetch

API = "https://api.github.com"

#: A change this size is not a fix, and this is a bounded, reviewed pipeline.
MAX_FILE_BYTES = 400_000
MAX_FILES = 20

#: Branches this platform creates. A fixed prefix makes them recognisable, and
#: refusing to write to anything else means a delivery cannot land on main.
BRANCH_PREFIX = "digital-brain/"


def headers(token):
    """Request headers, with authorization only when there is something to send.

    An empty token used to go out as ``Bearer `` and GitHub answered 401 - to a
    public repository, for a plain read. That was invisible while this module
    only ever wrote, because writing needs a credential anyway. The branch check
    reads, and Code Graph's own promise is that public repositories work without
    one, so an absent token has to mean anonymous rather than malformed.

    Nothing is granted by this: an empty string never authenticated anything.
    Writing still fails without the write credential, and readiness still
    requires it before an application can deliver.
    """
    sent = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }
    if token:
        sent["Authorization"] = f"Bearer {token}"
    return sent


def call(url, token, *, method="GET", payload=None, label="GitHub"):
    body = json.dumps(payload).encode() if payload is not None else None
    try:
        raw, _, _, _ = fetch(url, headers=headers(token), method=method, body=body)
    except FetchError as failure:
        error = ValidationError(f"{label}: {failure}")
        error.status = failure.status
        raise error from None
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        raise ValidationError(f"{label} returned a response that was not JSON.") from None


def safe_path(path):
    """A repository path a model may name, or a refusal.

    Refused rather than cleaned: a path that needs cleaning is a path nobody
    intended, and silently rewriting it is how a change lands somewhere else.
    """
    value = str(path or "").strip().replace("\\", "/")
    if not value or value.startswith("/") or ".." in value.split("/"):
        raise ValidationError(f"Refusing to write to {path!r}: not a repository-relative path.")
    if value.startswith(".git/") or value == ".git":
        raise ValidationError("Refusing to write inside .git.")
    if len(value) > 400:
        raise ValidationError("That path is too long to be a real one.")
    return value


def read_file(repository, path, ref, token):
    """One file's text and blob sha at a ref, or None when it does not exist."""
    path = safe_path(path)
    try:
        data = call(
            f"{API}/repos/{repository}/contents/{path}?ref={ref}", token, label="GitHub read"
        )
    except ValidationError:
        return None
    if not isinstance(data, dict) or data.get("type") != "file":
        return None
    if data.get("encoding") != "base64":
        return None
    try:
        content = base64.b64decode(data.get("content") or "")
    except (ValueError, TypeError):
        return None
    if len(content) > MAX_FILE_BYTES:
        return None
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        # Binary. Nothing here edits bytes it cannot read.
        return None
    return {"path": path, "text": text, "sha": data.get("sha")}


def absent_path(repository, path, ref, token):
    """The normalised path when GitHub says nothing is there, otherwise None.

    Deliberately not "read_file returned None": that also means too large,
    binary, or a directory, and treating any of those as absent would turn a
    creation into an overwrite of something nobody read. Only a 404 counts. A
    path `safe_path` refuses is not absent either -- it is not a path.
    """
    try:
        safe = safe_path(path)
    except ValidationError:
        return None
    try:
        call(f"{API}/repos/{repository}/contents/{safe}?ref={ref}", token, label="GitHub read")
    except ValidationError as failure:
        return safe if getattr(failure, "status", None) == 404 else None
    return None


def default_branch(repository, token):
    data = call(f"{API}/repos/{repository}", token, label="GitHub repository")
    branch = data.get("default_branch") if isinstance(data, dict) else None
    if not isinstance(branch, str) or not branch:
        raise ValidationError("GitHub did not report a default branch for that repository.")
    return branch


def branch_head(repository, branch, token):
    data = call(
        f"{API}/repos/{repository}/git/ref/heads/{branch}", token, label="GitHub branch"
    )
    sha = (data.get("object") or {}).get("sha") if isinstance(data, dict) else None
    if not isinstance(sha, str) or not sha:
        raise ValidationError(f"GitHub did not report a head commit for {branch}.")
    return sha


def create_branch(repository, name, from_sha, token):
    if not name.startswith(BRANCH_PREFIX):
        raise ValidationError("Refusing to create a branch outside this platform's prefix.")
    call(
        f"{API}/repos/{repository}/git/refs",
        token,
        method="POST",
        payload={"ref": f"refs/heads/{name}", "sha": from_sha},
        label="GitHub branch creation",
    )
    return name


def commit_file(repository, branch, path, text, sha, message, token, *, create=False):
    """Write one file on a branch this platform created.

    `sha` names the blob being replaced. Creating instead is a separate act the
    caller has to ask for by name, so a missing sha can never quietly become a
    new file: without `create` it is still refused, and with it a sha would mean
    the caller believes two contradictory things about the same path.
    """
    if not branch.startswith(BRANCH_PREFIX):
        raise ValidationError("Refusing to commit outside this platform's branch prefix.")
    if create and sha:
        raise ValidationError(f"Refusing to create {path}: it already has contents to replace.")
    if not create and not sha:
        raise ValidationError(f"Refusing to create {path}: only existing files are changed.")
    encoded = base64.b64encode(text.encode("utf-8")).decode()
    payload = {"message": message[:200], "content": encoded, "branch": branch}
    if sha:
        payload["sha"] = sha
    call(
        f"{API}/repos/{repository}/contents/{safe_path(path)}",
        token,
        method="PUT",
        payload=payload,
        label="GitHub commit",
    )


def check_runs(repository, ref, token):
    """What the repository's own CI made of a branch.

    Read-only, through the same hardened fetcher as every other call here. This
    platform never runs the code it writes; it asks GitHub what happened when
    GitHub ran it.
    """
    data = call(
        f"{API}/repos/{repository}/commits/{ref}/check-runs",
        token,
        label="GitHub checks",
    )
    runs = data.get("check_runs") if isinstance(data, dict) else None
    if not isinstance(runs, list):
        return []
    found = []
    for run in runs[:20]:
        if not isinstance(run, dict):
            continue
        found.append(
            {
                "name": str(run.get("name") or "check")[:120],
                # queued | in_progress | completed
                "status": str(run.get("status") or "")[:20],
                # success | failure | neutral | cancelled | timed_out | skipped
                "conclusion": str(run.get("conclusion") or "")[:20],
                "url": str(run.get("html_url") or "")[:500],
            }
        )
    return found


def open_pull_request(repository, branch, base, title, body, token):
    data = call(
        f"{API}/repos/{repository}/pulls",
        token,
        method="POST",
        payload={
            "title": title[:250],
            "head": branch,
            "base": base,
            "body": body[:60000],
            "draft": True,
        },
        label="GitHub pull request",
    )
    url = data.get("html_url") if isinstance(data, dict) else None
    if not isinstance(url, str) or not url.startswith("https://github.com/"):
        raise ValidationError("GitHub did not return a pull request address.")
    return url
