"""Code Factory delivered into a folder on this server instead of a pull request.

`apply_local` is the local `publish`: off unless `code_factory_local_write`,
approval rights required, every file checked against the snapshot before any
is written, each replaced atomically with its line endings kept, nothing
committed. `run_local_tests` is the one place this platform runs code it
wrote, and only on an approver's button press (CLAUDE.md). Split out of
`code_factory_build`, whose gate it goes through.
"""

import time

from django.core.exceptions import ValidationError
from django.utils import timezone

from .code_factory import note
from .code_factory_agents import local_repository
from .code_factory_build import gate
from .models import FactoryRun
from .services import audit


def local_targets(run, folder, changes):
    """[(absolute path, change)] once every change is safe to write, or a refusal.

    Checked as a whole before anything is written, so a refusal leaves the
    folder exactly as it was. The same rules a pull request is held to, in the
    terms a folder has:

    * a path stays inside the folder, and never into a hidden, dependency or
      build folder - `.git` above all;
    * a file the agents read from the snapshot must still say what the snapshot
      says, or somebody changed it since and this would overwrite their work;
    * a file the agents wrote as new must still be absent, for the same reason.
    """
    from .code_graph_local import _skipped

    held = dict(run.code_snapshot.files.values_list("path", "content"))
    planned, problems = [], []
    for change in changes:
        path = change["path"]
        parts = path.replace("\\", "/").split("/")
        if (
            not path
            or path.startswith(("/", "\\"))
            or ":" in parts[0]
            or any(part in ("", ".", "..") for part in parts)
            or any(_skipped(part) for part in parts[:-1])
            or parts[-1].startswith(".")
        ):
            problems.append(f"{path} is not a path this platform writes.")
            continue
        target = folder.joinpath(*parts)
        try:
            resolved = target.resolve()
        except (OSError, RuntimeError):
            problems.append(f"{path} could not be resolved.")
            continue
        if not resolved.is_relative_to(folder) or target.is_symlink():
            problems.append(f"{path} leads outside {folder}.")
            continue
        if target.exists():
            try:
                current = target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                problems.append(f"{path} could not be read to check it.")
                continue
            if path not in held:
                problems.append(
                    f"{path} exists, but the agents wrote it as a new file without "
                    "reading it. Writing it would replace a file nobody looked at."
                )
            elif current != held[path]:
                problems.append(
                    f"{path} has changed since snapshot v{run.code_snapshot.number}. "
                    "Refresh the index and implement the change again."
                )
        elif path in held:
            problems.append(f"{path} was removed from the folder after the snapshot.")
        planned.append((target, change))
    if problems:
        raise ValidationError(problems)
    return planned


def _write_text(target, content):
    """Replace one file in a single step, keeping its line endings."""
    import os
    import tempfile

    newline = "\n"
    if target.exists() and b"\r\n" in target.read_bytes()[:65536]:
        newline = "\r\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=".codefactory-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline=newline) as handle:
            handle.write(content)
        os.replace(temporary, target)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def apply_local(user, app_id, run_id):
    """Write a prepared change into the folder its code was read from.

    The local counterpart of `publish`, for a repository Code Graph reads from
    a folder on this server. Off unless an operator sets
    `code_factory_local_write`, and refused under production. Like `publish` it
    re-reads the prepared files rather than taking them from the caller, so what
    lands is what was shown. Nothing is committed: the folder's own tools
    decide what happens next, and its git history is untouched.
    """
    from django.conf import settings

    from .code_graph_ingest import local_folder

    app, run, _ = gate(user, app_id, run_id)
    if not settings.CODE_FACTORY_LOCAL_WRITE:
        raise ValidationError(
            "Writing to a folder on this server is not enabled. Set "
            "code_factory_local_write = true in the configuration to allow it."
        )
    repository = local_repository(run)
    if repository is None:
        raise ValidationError("This run's code was not read from a folder on this server.")
    if run.delivered:
        raise ValidationError("This change has already been delivered.")
    if run.status != "prepared":
        raise ValidationError("Implement the change and read its summary first.")
    changes = [{"path": item.path, "content": item.content} for item in run.changes.all()]
    if not changes:
        raise ValidationError("This run has no prepared change to write.")
    folder = local_folder(repository)
    claimed = FactoryRun.objects.filter(pk=run.pk, status="prepared").update(
        status="delivering", error=""
    )
    if not claimed:
        raise ValidationError("This run is already being delivered.")
    run.refresh_from_db()
    note(run, f"Writing to {folder} approved by {user.get_username()}.", level="check")
    try:
        planned = local_targets(run, folder, changes)
    except ValidationError:
        note(run, "Nothing was written to the folder.", level="problem")
        FactoryRun.objects.filter(pk=run.pk).update(status="prepared")
        raise
    try:
        for target, change in planned:
            _write_text(target, change["content"])
            note(run, f"Wrote {change['path']}.", phase="delivery")
    except OSError as failure:
        note(run, f"Writing stopped: {failure.strerror or failure}.", level="problem")
        FactoryRun.objects.filter(pk=run.pk).update(
            status="failed", error="A file could not be written. See the run log."
        )
        raise ValidationError("A file could not be written. See the run log.") from None
    FactoryRun.objects.filter(pk=run.pk).update(
        status="delivered", delivered_to=str(folder)[:500], finished_at=timezone.now()
    )
    audit(
        user,
        "factory.change_written_to_folder",
        run.pk,
        app.product.portfolio.organization,
        details={"folder": str(folder), "files": len(planned)},
    )
    note(
        run,
        f"{len(planned)} file(s) written to {folder}. Nothing was committed; review "
        "and commit them with the folder's own tools.",
        level="result",
    )
    return folder


#: How long a folder's own test suite may run before it is stopped.
LOCAL_TEST_TIMEOUT = 300
#: What the run's check is called, so it reads as a run here and not as CI.
LOCAL_TEST_CHECK = "pytest · run on this server"


def folder_python(folder):
    """The folder's own virtual environment's interpreter, or None.

    Its own, never this server's: the tests need the folder's dependencies, and
    running them under the platform's interpreter would test against ours.
    """
    venv = folder / ".venv"
    for candidate in (venv / "Scripts" / "python.exe", venv / "bin" / "python"):
        if candidate.is_file():
            return candidate
    return None


def run_local_tests(user, app_id, run_id):
    """Run the folder's test suite once a change has been written into it.

    The one place this platform runs code, and it runs it only here: off unless
    `code_factory_local_tests` is set, refused under production, and only when
    a person with approval rights presses the button. The suite runs with the
    folder's own interpreter, a minimal environment - no secrets directory, no
    credential variables - and a time limit. Its verdict is recorded where a
    pull request's CI verdict would be, so the Tests stage reads the same.
    """
    import os
    import subprocess

    from django.conf import settings

    from .code_graph_ingest import local_folder

    app, run, _ = gate(user, app_id, run_id)
    if not settings.CODE_FACTORY_LOCAL_TESTS:
        raise ValidationError(
            "Running tests on this server is not enabled. Set "
            "code_factory_local_tests = true in the configuration to allow it."
        )
    repository = local_repository(run)
    if repository is None or not run.delivered_to:
        raise ValidationError("Write the change to the folder before running its tests.")
    folder = local_folder(repository)
    setup = run.code_snapshot.test_setup or {}
    if setup.get("python") != "pytest":
        raise ValidationError(
            "Only pytest suites can be run here so far, and this folder does not declare one."
        )
    python = folder_python(folder)
    if python is None:
        raise ValidationError(
            f"{repository.name} has no .venv to run its tests with. Create one in the "
            "folder and install its dependencies, then try again."
        )
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "SYSTEMROOT", "SystemRoot", "COMSPEC", "TEMP", "TMP", "LANG"}
    }
    environment.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"})
    FactoryRun.objects.filter(pk=run.pk).update(
        checks=[{"name": LOCAL_TEST_CHECK, "status": "in_progress", "conclusion": None}],
        checks_read_at=timezone.now(),
    )
    note(run, f"{user.get_username()} ran {repository.name}'s tests on this server.", level="check")
    started = time.monotonic()
    try:
        finished = subprocess.run(
            [str(python), "-m", "pytest", "-q", "-p", "no:cacheprovider", "--color=no"],
            cwd=folder,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=LOCAL_TEST_TIMEOUT,
            check=False,
        )
        lines = [line for line in (finished.stdout or "").splitlines() if line.strip()]
        summary = lines[-1].strip("= ").strip() if lines else "pytest reported nothing."
        conclusion = "success" if finished.returncode == 0 else "failure"
        tail = "\n".join(lines[-12:])
    except subprocess.TimeoutExpired:
        summary = f"Stopped after {LOCAL_TEST_TIMEOUT} seconds."
        conclusion, tail = "timed_out", ""
    except OSError as failure:
        summary = f"The tests could not be started: {failure.strerror or failure}."
        conclusion, tail = "failure", ""
    seconds = round(time.monotonic() - started, 1)
    FactoryRun.objects.filter(pk=run.pk).update(
        checks=[
            {
                "name": LOCAL_TEST_CHECK,
                "status": "completed",
                "conclusion": conclusion,
                "summary": f"{summary} ({seconds}s)"[:300],
                "url": "",
            }
        ],
        checks_read_at=timezone.now(),
    )
    note(
        run,
        f"Tests {'passed' if conclusion == 'success' else conclusion}: {summary}"
        + (f"\n{tail}" if tail and conclusion != "success" else ""),
        phase="tests",
        level="result" if conclusion == "success" else "problem",
    )
    audit(
        user,
        "factory.local_tests_run",
        run.pk,
        app.product.portfolio.organization,
        details={"folder": str(folder), "conclusion": conclusion, "summary": summary[:200]},
    )
    return conclusion, summary
