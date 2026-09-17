"""Build the mixed-format document set in `demo-artifacts/docs/`.

    .venv/Scripts/python.exe build_formats.py

Every document exists in **one** format, chosen to match how that kind of
document actually lives in a health network: a signed business requirements
document is a Word file, a traceability matrix is a spreadsheet, an architecture
page is an intranet export, a runbook is plain text, an ADR is Markdown in the
repository beside the code it describes.

Nothing is duplicated across formats. Two copies of one document would be two
sources in the knowledge graph saying the same thing, and a citation would point
at whichever was indexed first.

Markdown sources live in `tools/source/`. They are the editable originals and
are **not** meant to be loaded into Digital Brain - loading both them and the
built set is the duplication this file exists to avoid.
"""

import shutil
import sys
from pathlib import Path

import decks
import renderers
from mdparse import parse

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "source"
DOCS = HERE.parent / "docs"

#: source path -> output format. A format of "md" copies the file through.
#: The rationale column is the reason the format was chosen, not decoration:
#: it is what a customer asks about when they see the mixture.
MANIFEST: list[tuple[str, str, str]] = [
    ("00-overview/sdlc-map.md", "pptx", "Steering overview, presented rather than read"),
    ("01-requirements/business-requirements.md", "docx", "Signed off in Word by the board"),
    ("01-requirements/user-stories.md", "docx", "Circulated to the delivery team"),
    ("01-requirements/non-functional-requirements.md", "xlsx", "A requirements matrix is a grid"),
    ("01-requirements/regulatory-requirements.md", "pdf", "Issued by governance, read-only"),
    ("02-design/architecture.md", "html", "Intranet page export"),
    ("02-design/data-model.md", "md", "Lives beside the schema in the repository"),
    ("02-design/security-design.md", "docx", "Security review document"),
    ("02-design/adr/0001-sqlite-for-release-one.md", "md", "ADRs belong in the repository"),
    ("02-design/adr/0002-static-token-directory.md", "md", "ADRs belong in the repository"),
    ("02-design/adr/0003-transparent-in-process-scoring.md", "md", "ADRs belong in the repository"),
    ("02-design/adr/0004-fhir-profile-scope.md", "md", "ADRs belong in the repository"),
    ("03-build/coding-standards.md", "md", "Read in the editor, next to the code"),
    ("03-build/api-specification.md", "html", "Published API documentation"),
    ("04-test/test-strategy.md", "docx", "Approved by the QA lead"),
    ("04-test/test-plan.md", "docx", "Executed and signed"),
    ("04-test/traceability-matrix.md", "xlsx", "An RTM is always a spreadsheet"),
    ("05-release/release-plan.md", "docx", "Tabled at the release review"),
    ("05-release/deployment-runbook.md", "md", "Followed from the repository during a deploy"),
    ("05-release/release-notes-1.0.0.md", "html", "Published to readers"),
    ("06-operate/service-level-objectives.md", "xlsx", "Targets and actuals, tracked monthly"),
    ("06-operate/operations-runbook.md", "txt", "Opened on a pager at three in the morning"),
    ("06-operate/incident-2026-08-14-postmortem.md", "docx", "Circulated after the review"),
    ("07-govern/hipaa-control-matrix.md", "xlsx", "A control matrix is a grid"),
    ("07-govern/risk-register.md", "xlsx", "A risk register is a grid"),
    ("07-govern/change-management.md", "docx", "Policy document"),
]

WRITERS = {
    "docx": renderers.write_docx,
    "xlsx": renderers.write_xlsx,
    "html": renderers.write_html,
    "txt": renderers.write_txt,
    "pdf": renderers.write_pdf,
    "pptx": decks.write_deck,
}


# ------------------------------------------------- the deck that was born a deck

READINESS_REVIEW = [
    {
        "heading": "Purpose",
        "bullets": [
            "Decide whether CarePath 1.0.0 releases to the pilot on 4 June",
            "Pilot scope: 400 patients, two clinics, behind an IP allow-list",
            "Audit remediation commitment (C-04) falls due 30 June",
        ],
        "notes": (
            "The remediation date is the pressure in the room. Everyone knows it and "
            "nobody says it out loud."
        ),
    },
    {
        "heading": "Quality gates",
        "rows": [
            ["Gate", "Target", "Actual", "Verdict"],
            ["Tests passing", "all", "111 / 111", "Pass"],
            ["Line coverage", "85%", "91%", "Pass"],
            ["Lint", "clean", "clean", "Pass"],
            ["FRs with a test", "12 / 12", "12 / 12", "Pass"],
            ["Sev 1-2 defects open", "0", "0", "Pass"],
            ["High-severity findings open", "0", "3", "FAIL"],
        ],
        "notes": (
            "Five green rows and one red one. The five are quantitative and the red one "
            "is not, which is roughly how it was read."
        ),
    },
    {
        "heading": "Open findings",
        "rows": [
            ["Ref", "Finding", "Severity"],
            ["T-01", "Patient search accepts an unbounded limit", "High"],
            ["T-02", "Patient name written to the application log", "High"],
            ["T-03", "Export does not consult consent before disclosure", "High"],
        ],
        "notes": (
            "Three findings, presented on one slide, discussed as one item. T-03 is the "
            "only one that defeats a business requirement rather than a non-functional "
            "one. Nothing on this slide says so."
        ),
    },
    {
        "heading": "Recommendation",
        "bullets": [
            "Release to pilot with waiver W-01 covering all three findings",
            "Exposure is bounded: 400 patients, two clinics, IP allow-list",
            "All three scheduled for 1.0.1",
            "Waiver expires at general availability",
        ],
        "notes": (
            "One rationale for three findings. It addresses exposure - who can reach the "
            "service - and not blast radius, which is what one authorised user can do "
            "once they are inside. T-01 became INC-2026-0814 ten weeks later for exactly "
            "that reason."
        ),
    },
    {
        "heading": "Decision",
        "bullets": [
            "Approved: release to pilot on 2026-06-04 under waiver W-01",
            "Proposed: Delivery Manager · Seconded: Clinical Director",
            "No objections recorded",
            "Actions: none raised",
        ],
        "notes": (
            "The minute records no discussion of T-03 specifically. This slide is the "
            "whole governance failure, in four bullets, approved by people doing their "
            "jobs properly with the information in front of them."
        ),
    },
]

BOARD_CSV = [
    ["Issue key", "Issue Type", "Summary", "Status", "Priority", "Reporter", "Labels",
     "Affects Version/s", "Fix Version/s", "Requirement", "Traceability", "Risk"],
    ["CARE-401", "Bug", "Patient consent is not checked before a record is exported to a partner",
     "Open", "Highest", "Adaeze Nwosu", "regulatory consent release-1.0.1 waived-w01",
     "1.0.0", "1.0.1", "BR-05", "T-03", "R-01"],
    ["CARE-412", "Bug", "Patient search accepts an unbounded limit",
     "Open", "High", "Sam Ferreira", "availability incident-follow-up release-1.0.1 waived-w01",
     "1.0.0", "1.0.1", "NFR-06", "T-01", "R-04"],
    ["CARE-418", "Bug", "Patient name and date of birth are written to the application log",
     "Open", "High", "Priya Raghavan", "privacy logging release-1.0.1 waived-w01",
     "1.0.0", "1.0.1", "NFR-04", "T-02", "R-03"],
    ["CARE-425", "Bug", "Referral status changes are not written to the audit trail",
     "Open", "Medium", "Adaeze Nwosu", "regulatory audit release-1.0.1",
     "1.0.0", "1.0.1", "NFR-03", "T-04", "R-02"],
    ["CARE-431", "Bug", "An auditor receives the full patient record, including telephone and address",
     "Open", "Medium", "Adaeze Nwosu", "regulatory minimum-necessary release-1.1",
     "1.0.0", "1.1", "REG-03", "F-03", ""],
    ["CARE-437", "Story", "Rate limit the authentication path",
     "Open", "Medium", "Tomas Beck", "security nfr-12 release-1.1",
     "", "1.1", "NFR-12", "T-05", "R-05"],
    ["CARE-444", "Task", "Operations runbook names a deployment that was retired",
     "Open", "Low", "Sam Ferreira", "documentation operational-readiness",
     "1.0.0", "", "", "", "R-11"],
]


def build() -> int:
    if not SOURCE.exists():
        print(f"No source directory at {SOURCE}", file=sys.stderr)
        return 1

    for path in sorted(DOCS.rglob("*")):
        if path.is_file():
            path.unlink()

    built: list[tuple[str, str]] = []
    for relative, fmt, _rationale in MANIFEST:
        origin = SOURCE / relative
        if not origin.exists():
            print(f"missing source: {relative}", file=sys.stderr)
            return 1
        target = (DOCS / relative).with_suffix("." + fmt)
        target.parent.mkdir(parents=True, exist_ok=True)
        if fmt == "md":
            shutil.copyfile(origin, target)
        else:
            blocks = parse(origin.read_text(encoding="utf-8"))
            WRITERS[fmt](blocks, target, origin.stem.replace("-", " ").title())
        built.append((str(target.relative_to(DOCS)).replace("\\", "/"), fmt))

    deck = DOCS / "05-release" / "release-readiness-review.pptx"
    decks.build_slides(
        deck,
        "CarePath 1.0.0 — release readiness review",
        "Clinical Systems Board · 2026-06-02",
        READINESS_REVIEW,
    )
    built.append(("05-release/release-readiness-review.pptx", "pptx"))

    board = HERE.parent / "demo-kit" / "CARE-board-export.csv"
    renderers.write_csv(BOARD_CSV, board)
    built.append(("../demo-kit/CARE-board-export.csv", "csv"))

    counts: dict[str, int] = {}
    for _name, fmt in built:
        counts[fmt] = counts.get(fmt, 0) + 1
    for name, fmt in built:
        print(f"  {fmt:5} {name}")
    print()
    print(f"{len(built)} files, {len(counts)} formats: " + ", ".join(
        f"{fmt} x{count}" for fmt, count in sorted(counts.items())
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(build())
