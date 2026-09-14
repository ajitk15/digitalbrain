"""Write the project's runtime dependencies out as a pip requirements file.

Only used by the pip fallback in scripts/environment.ps1, for a machine where uv
could not be installed. `pip install -e .` is not an option here: this is an
application, not a distributable package - there is no [build-system] table, and
setuptools' auto-discovery finds several top-level directories and refuses to
guess. uv does not care, because it treats the project as non-packaged.

So the dependencies are installed directly instead. pyproject.toml stays the only
place they are declared; this just restates them in the one format pip accepts.
The result is written to a file rather than stdout on purpose - redirecting with
`>` in Windows PowerShell produces UTF-16, which pip cannot read.

Note the versions here are pyproject's ranges, not uv.lock's exact pins. The
fallback path says so out loud; use uv for a reproducible environment.
"""

import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    with (ROOT / "pyproject.toml").open("rb") as source:
        dependencies = tomllib.load(source)["project"]["dependencies"]
    if not dependencies:
        sys.stderr.write("pyproject.toml declares no dependencies; refusing to write.\n")
        return 1
    target = ROOT / ".runtime" / "requirements.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Generated from pyproject.toml by scripts/export_requirements.py.\n"
        "# Do not edit, and do not commit: pyproject.toml is the source of truth.\n"
    )
    target.write_text(header + "\n".join(dependencies) + "\n", encoding="utf-8")
    print(f"Wrote {len(dependencies)} dependencies to {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
