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


#: Requirement files a folder's .venv is filled from, in the order offered.
REQUIREMENT_FILES = (
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-test.txt",
    "dev-requirements.txt",
    "test-requirements.txt",
)


#: How long setting up a folder's .venv may take, every step together.
LOCAL_SETUP_TIMEOUT = 900
#: Variables an installer is given besides the ones the tests get: where the
#: person's own pip/uv configuration and caches are, a proxy, a CA bundle. No
#: credential variable, no secrets directory, nothing of this platform's.
SETUP_VARIABLES = {
    "PATH", "PATHEXT", "SYSTEMROOT", "SystemRoot", "SYSTEMDRIVE", "COMSPEC", "TEMP",
    "TMP", "LANG", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA",
    "LOCALAPPDATA", "PROGRAMDATA", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
}


def venv_python():
    """The .venv interpreter, relative to the folder, for this server's system."""
    import sys

    return r".venv\Scripts\python.exe" if sys.platform == "win32" else ".venv/bin/python"


def setup_steps(folder):
    """The commands that give the folder a .venv its tests can use, as argv lists.

    Read from what the folder declares: `uv sync` for a uv.lock, otherwise a
    venv made by the system's Python, filled from each requirements file (or
    the project itself from pyproject.toml), and pytest last in every case.
    """
    import sys

    python = venv_python()
    pip = [python, "-m", "pip", "install"]
    if (folder / "uv.lock").is_file():
        return [["uv", "sync"], [*pip, "pytest"]]
    steps = [["py" if sys.platform == "win32" else "python3", "-m", "venv", ".venv"]]
    found = [name for name in REQUIREMENT_FILES if (folder / name).is_file()]
    steps += [[*pip, "-r", name] for name in found]
    if not found and (folder / "pyproject.toml").is_file():
        steps.append([*pip, "-e", "."])
    steps.append([*pip, "pytest"])
    return steps


def setup_commands(folder):
    """The same steps as lines a person could type, shown beside the button."""
    return [f'cd "{folder}"', *(" ".join(step) for step in setup_steps(folder))]


def set_up_environment(user, app_id, run_id):
    """Make the folder's .venv and install its dependencies, on an approver's press.

    The second thing this platform runs in a folder, and run under the same
    rules as its tests: off unless `code_factory_local_tests` (refused under
    production), approval rights, a change already written there, a trimmed
    environment and a time limit. It runs installers, which execute the
    packages' own build code and download from an index - which is why it is a
    button a person presses and never a step a run takes by itself. The venv is
    made by the system's Python, never this server's interpreter. Returns
    (ok, summary); each step is in the run log.
    """
    import os
    import shutil
    import subprocess
    import sys
    from pathlib import Path

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
        raise ValidationError("Write the change to the folder before setting up its tests.")
    if (run.code_snapshot.test_setup or {}).get("python") != "pytest":
        raise ValidationError(
            "Only pytest suites can be run here so far, and this folder does not declare one."
        )
    folder = local_folder(repository)
    environment = {key: value for key, value in os.environ.items() if key in SETUP_VARIABLES}
    environment.update(
        {
            "PYTHONIOENCODING": "utf-8",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
            # uv would otherwise use an active environment of this server's.
            "UV_PROJECT_ENVIRONMENT": str(folder / ".venv"),
        }
    )
    ours = Path(sys.executable).resolve()
    note(run, f"{user.get_username()} set up {repository.name}'s .venv.", level="check")
    deadline = time.monotonic() + LOCAL_SETUP_TIMEOUT
    for step in setup_steps(folder):
        shown = " ".join(step)
        if step[0].startswith(".venv"):
            program = str(folder / step[0])
        else:
            program = shutil.which(step[0], path=environment.get("PATH"))
            if program is None:
                summary = f"{step[0]} was not found on this server's PATH."
                note(run, f"Setup stopped: {summary}", phase="tests", level="problem")
                return False, summary
            if Path(program).resolve() == ours:
                summary = "The only Python found is this server's own; install a system Python."
                note(run, f"Setup stopped: {summary}", phase="tests", level="problem")
                return False, summary
        remaining = deadline - time.monotonic()
        try:
            finished = subprocess.run(
                [program, *step[1:]],
                cwd=folder,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=max(remaining, 1),
                check=False,
            )
        except subprocess.TimeoutExpired:
            summary = f"Stopped after {LOCAL_SETUP_TIMEOUT} seconds at: {shown}"
            note(run, f"Setup stopped: {summary}", phase="tests", level="problem")
            return False, summary
        except OSError as failure:
            summary = f"{shown} could not be started: {failure.strerror or failure}."
            note(run, f"Setup stopped: {summary}", phase="tests", level="problem")
            return False, summary
        if finished.returncode != 0:
            output = (finished.stderr or "") + (finished.stdout or "")
            lines = [line for line in output.splitlines() if line.strip()]
            summary = f"{shown} failed (exit {finished.returncode})."
            note(
                run,
                f"Setup stopped: {summary}" + ("\n" + "\n".join(lines[-12:]) if lines else ""),
                phase="tests",
                level="problem",
            )
            audit(
                user,
                "factory.local_environment_set_up",
                run.pk,
                app.product.portfolio.organization,
                details={"folder": str(folder), "ok": False, "step": shown[:200]},
            )
            return False, summary
        note(run, f"Ran {shown}.", phase="tests")
    audit(
        user,
        "factory.local_environment_set_up",
        run.pk,
        app.product.portfolio.organization,
        details={"folder": str(folder), "ok": True},
    )
    summary = f"{repository.name}'s .venv is set up."
    note(run, summary, phase="tests", level="result")
    return True, summary


def test_environment(run):
    """Whether a written folder run has its own .venv, and how to make one if not.

    None for anything but a folder run whose change was written there and whose
    folder declares a pytest suite - the only runs whose tests this server runs.
    """
    from .code_graph_ingest import local_folder

    if not run.tests_required:
        return None
    repository = local_repository(run)
    if repository is None:
        return None
    try:
        folder = local_folder(repository)
    except ValidationError:
        return None
    ready = folder_python(folder) is not None
    return {
        "ready": ready,
        "folder": str(folder),
        "commands": [] if ready else setup_commands(folder),
    }


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
        note(
            run,
            f"{repository.name} has no .venv yet, so its tests were not run. The Tests "
            "stage lists the commands that make one.",
            phase="tests",
            level="problem",
        )
        raise ValidationError(
            f"{repository.name} has no .venv to run its tests with. Set one up with the "
            "commands on the Tests stage, then run the tests again."
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
