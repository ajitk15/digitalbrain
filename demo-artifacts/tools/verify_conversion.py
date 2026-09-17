"""Convert every built artifact with the platform's own converter and report.

This is the check that matters. It does not use a library of its own: it runs
`scripts/extract_text.py` as a subprocess, exactly as `platform_core.processing`
does, with the same suffix argument. If a file converts here it converts on
upload, and if it does not, the demonstration would have failed in front of a
customer instead of here.

    .venv/Scripts/python.exe verify_conversion.py [path-to-digitalbrain]

The platform's interpreter is used for the conversion, because MarkItDown lives
in its environment rather than this one.
"""

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ARTIFACTS = HERE.parent
DEFAULT_ROOT = ARTIFACTS.parent

#: Extensions the platform lists as convertible in `fetching.CONTENT_SUFFIXES`.
CONVERTIBLE = {
    ".docx", ".xlsx", ".pptx", ".pdf", ".html", ".txt", ".md", ".csv", ".json", ".xml",
}

#: What each file must still say after conversion. A file that converts to
#: something is not the same as a file that converts to the right thing: an
#: empty spreadsheet converts perfectly and carries nothing.
EXPECTED: dict[str, str] = {
    "01-requirements/business-requirements.docx": "BR-05",
    "01-requirements/user-stories.docx": "US-10",
    "01-requirements/non-functional-requirements.xlsx": "NFR-06",
    "01-requirements/regulatory-requirements.pdf": "REG-03",
    "02-design/architecture.html": "audit",
    "02-design/data-model.md": "append-only",
    "02-design/security-design.docx": "STRIDE",
    "03-build/api-specification.html": "everything",
    "04-test/test-strategy.docx": "NFR-12",
    "04-test/test-plan.docx": "TC-3",
    "04-test/traceability-matrix.xlsx": "T-03",
    "05-release/release-plan.docx": "W-01",
    "05-release/release-notes-1.0.0.html": "consent",
    "05-release/release-readiness-review.pptx": "T-03",
    "06-operate/service-level-objectives.xlsx": "error budget",
    "06-operate/operations-runbook.txt": "carepath-api-blue",
    "06-operate/incident-2026-08-14-postmortem.docx": "INC-2026-0814",
    "07-govern/hipaa-control-matrix.xlsx": "164.502",
    "07-govern/risk-register.xlsx": "R-01",
    "07-govern/change-management.docx": "waiver",
    "00-overview/sdlc-map.pptx": "BR-05",
}


def convert(script: Path, python: Path, path: Path) -> tuple[bool, str]:
    result = subprocess.run(
        [str(python), str(script), str(path), path.suffix.lower()],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        return False, result.stderr.decode("utf-8", "replace").strip().split("\n")[-1][:120]
    return True, result.stdout.decode("utf-8", "replace")


def main() -> int:
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_ROOT
    script = root / "scripts" / "extract_text.py"
    python = root / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        python = root / ".venv" / "bin" / "python"
    if not script.exists() or not python.exists():
        print(f"Platform not found under {root}", file=sys.stderr)
        return 2

    targets = sorted(
        path
        for path in list((ARTIFACTS / "docs").rglob("*")) + list((ARTIFACTS / "demo-kit").glob("*"))
        if path.is_file() and path.suffix.lower() in CONVERTIBLE
    )

    failures = 0
    missing = 0
    print(f"{'file':62} {'fmt':5} {'chars':>7}  check")
    print("-" * 92)
    for path in targets:
        relative = path.relative_to(ARTIFACTS).as_posix()
        ok, output = convert(script, python, path)
        if not ok:
            print(f"{relative:62} {path.suffix[1:]:5} {'-':>7}  CONVERSION FAILED: {output}")
            failures += 1
            continue
        key = path.relative_to(ARTIFACTS / "docs").as_posix() if "docs/" in relative else ""
        needle = EXPECTED.get(key, "")
        verdict = "ok"
        if not output.strip():
            verdict = "EMPTY"
            failures += 1
        elif needle and needle.lower() not in output.lower():
            verdict = f"MISSING {needle!r}"
            missing += 1
        print(f"{relative:62} {path.suffix[1:]:5} {len(output):>7}  {verdict}")

    print("-" * 92)
    print(f"{len(targets)} files, {failures} conversion failures, {missing} content checks missed")
    return 1 if (failures or missing) else 0


if __name__ == "__main__":
    raise SystemExit(main())
