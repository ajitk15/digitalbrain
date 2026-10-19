"""Seed the persistent VPS demo repository once; redeploys preserve approved edits."""

import os
import shutil
from pathlib import Path

SEED = Path("/opt/digitalbrain/demo-seed/carepath")
WORKSPACE = Path("/var/lib/digitalbrain/demo")
INTERPRETER = Path("/opt/carepath-venv")


def initialize(seed=SEED, workspace=WORKSPACE, interpreter=INTERPRETER):
    # Only the migration container (or the all-in-one container) calls this.
    # flock also serializes overlapping deploys against the same named volume.
    import fcntl

    workspace.mkdir(parents=True, exist_ok=True)
    with (workspace / ".seed.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        repository = workspace / "carepath"
        if not repository.exists():
            temporary = workspace / f".carepath-{os.getpid()}"
            try:
                shutil.copytree(seed, temporary)
                temporary.rename(repository)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
            print(f"demo: initialized {repository}")
        # Keep the interpreter in the immutable image, so a dependency update
        # takes effect on redeploy without replacing the repository's files.
        venv = repository / ".venv"
        if not venv.is_symlink() and venv.exists():
            raise RuntimeError(f"{venv} must be a symlink to the image's test environment.")
        if not venv.is_symlink():
            venv.symlink_to(interpreter, target_is_directory=True)
        elif venv.readlink() != interpreter:
            raise RuntimeError(f"{venv} points to an unexpected test environment.")


if __name__ == "__main__":
    initialize()
