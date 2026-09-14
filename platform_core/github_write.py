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

**Only files that already exist.** Paths come from a model, so a change may only
replace a file the platform first read from the repository at the branch it is
about to write to. Nothing is created, nothing is deleted, and a path that did
not come back from a read is refused rather than sanitised.
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
    return {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def call(url, token, *, method="GET", payload=None, label="GitHub"):
    body = json.dumps(payload).encode() if payload is not None else None
    try:
        raw, _, _, _ = fetch(url, headers=headers(token), method=method, body=body)
    except FetchError as failure:
        raise ValidationError(f"{label}: {failure}") from None
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


def commit_file(repository, branch, path, text, sha, message, token):
    """Replace one existing file on a branch this platform created."""
    if not branch.startswith(BRANCH_PREFIX):
        raise ValidationError("Refusing to commit outside this platform's branch prefix.")
    if not sha:
        raise ValidationError(f"Refusing to create {path}: only existing files are changed.")
    encoded = base64.b64encode(text.encode("utf-8")).decode()
    call(
        f"{API}/repos/{repository}/contents/{safe_path(path)}",
        token,
        method="PUT",
        payload={
            "message": message[:200],
            "content": encoded,
            "sha": sha,
            "branch": branch,
        },
        label="GitHub commit",
    )


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
