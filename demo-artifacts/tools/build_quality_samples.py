"""Build the CarePath source-quality samples: one planted problem per file.

    .venv/Scripts/python.exe demo-artifacts/tools/build_quality_samples.py

Writes `../docs/08-quality-samples/`, beside the CarePath documents, so the
scorecard reads them too: they are meant to score badly, and the baseline was
set with them in. `build_formats.py` leaves that folder alone. Uses only what
the platform already installs - Pillow, openpyxl, pypdf and the standard
library - so the .docx and the text-layer PDF are written by hand. What each
file should show is in `quality-samples.md`.
"""

import zipfile
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from openpyxl import Workbook
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader, PdfWriter

OUT = Path(__file__).resolve().parent.parent / "docs" / "08-quality-samples"
NOTICE = "Demonstration artifact. Synthetic content only - not a clinical record."


# ------------------------------------------------------------------ .docx by hand

def _run(text, bold=False):
    props = "<w:rPr><w:b/></w:rPr>" if bold else ""
    return f'<w:r>{props}<w:t xml:space="preserve">{escape(text)}</w:t></w:r>'


def _paragraph(text, style=None, bold=False):
    props = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f"<w:p>{props}{_run(text, bold)}</w:p>"


def _table(rows, bold_first):
    """No row is marked as a header row (w:tblHeader): how most Word tables are made."""
    out = ['<w:tbl><w:tblPr><w:tblBorders>'
           + "".join(f'<w:{side} w:val="single" w:sz="4"/>'
                     for side in ("top", "left", "bottom", "right", "insideH", "insideV"))
           + "</w:tblBorders></w:tblPr>"]
    for index, row in enumerate(rows):
        out.append("<w:tr>")
        for cell in row:
            out.append(f"<w:tc>{_paragraph(cell, bold=bold_first and index == 0)}</w:tc>")
        out.append("</w:tr>")
    out.append("</w:tbl>")
    return "".join(out)


def write_docx(target, body):
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        + "".join(
            f'<w:style w:type="paragraph" w:styleId="{name}"><w:name w:val="{label}"/></w:style>'
            for name, label in (("Title", "Title"), ("Heading1", "heading 1"))
        )
        + "</w:styles>"
    )
    parts = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/'
            'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '<Override PartName="/word/styles.xml" ContentType="application/'
            'vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
            "</Types>"
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
            '2006/relationships/officeDocument" Target="word/document.xml"/>'
            "</Relationships>"
        ),
        "word/_rels/document.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
            '2006/relationships/styles" Target="styles.xml"/>'
            "</Relationships>"
        ),
        "word/document.xml": document,
        "word/styles.xml": styles,
    }
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in parts.items():
            archive.writestr(zipfile.ZipInfo(name, (2026, 10, 1, 0, 0, 0)), text)


def discharge_checklist():
    body = "".join([
        _paragraph("Discharge checklist - CarePath 1.0", "Title"),
        _paragraph("Document: OPS-CAREPATH-011 - Version 1.0 - Status: Active - "
                   "Owner: Ward Manager, Riverside General - 2026-09-01"),
        _paragraph(NOTICE),
        _paragraph("Before the patient leaves", "Heading1"),
        _paragraph("The first table was made in Word with its header row in bold but not "
                   "marked as a header row. The platform reads the bold row as the header."),
        _table([["Step", "Owner", "Recorded in"],
                ["Medicines reconciled", "Pharmacist", "Discharge summary"],
                ["Referral raised (BR-02)", "Care coordinator", "CarePath referral"],
                ["Follow-up booked", "Ward clerk", "Clinic system"]], bold_first=True),
        _paragraph("After the patient leaves", "Heading1"),
        _paragraph("The second table's first row is not bold either, so nothing says which "
                   "row names the columns: the graph can only call them column 1, 2 and 3."),
        _table([["Check", "Within", "Escalate to"],
                ["Referral acknowledged", "1 working day", "Care coordinator"],
                ["Patient contacted", "2 working days", "Community nurse"],
                ["Readmission risk reviewed", "7 days", "Clinical lead"]], bold_first=False),
    ])
    write_docx(OUT / "discharge-checklist.docx", body)


# ------------------------------------------------------------------ spreadsheet

def bed_capacity():
    """A raw export: no column names, repeated rows, gaps."""
    book = Workbook()
    sheet = book.active
    sheet.title = "Export"
    rows = [
        [None, None, None, None],
        ["Ward 4", 28, 26, None],
        ["Ward 5", 30, None, None],
        ["Ward 4", 28, 26, None],
        ["Ward 7", None, None, "closed"],
        ["Ward 5", 30, None, None],
    ]
    for row in rows:
        sheet.append(row)
    book.save(OUT / "bed-capacity-export.xlsx")


# ------------------------------------------------------------------ scans

def _font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def page_image(lines, size=(1700, 2200)):
    """A page as a scanner would deliver it: a picture of text, no text layer."""
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    y = 160
    for line in lines:
        font = _font(54 if line.startswith("#") else 40)
        draw.text((140, y), line.lstrip("# "), fill="black", font=font)
        y += 90 if line.startswith("#") else 64
    return image


REFERRAL_SLA = [
    ["# Referral service levels", "Riverside Health Network - CarePath 1.0",
     "Document REQ-CAREPATH-009  Version 1.0  2026-08-20", "",
     "Urgent referrals are acknowledged within 4 hours.",
     "Routine referrals are acknowledged within 1 working day.",
     "An unacknowledged referral escalates to the care coordinator.",
     "This answers BR-02 and the availability objective NFR-06."],
    ["# Escalation", "",
     "Coordinators review open referrals at 09:00 and 15:00.",
     "A referral open for 3 working days is reported to the clinical lead.",
     "Northvale referrals follow the same timings (A-02)."],
]


def referral_sla_scanned():
    pages = [page_image(lines) for lines in REFERRAL_SLA]
    pages[0].save(OUT / "referral-sla-scanned.pdf", save_all=True,
                  append_images=pages[1:], resolution=200)


def _text_pdf(lines):
    """A one-page PDF with a real text layer, written by hand."""
    stream = ["BT", "/F1 12 Tf", "72 760 Td", "16 TL"]
    for line in lines:
        text = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream.append(f"({text}) Tj T*")
    stream.append("ET")
    content = "\n".join(stream).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content
        + b"\nendstream",
    ]
    out = BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
              .encode())
    return out.getvalue()


ATTESTATION = ["# Attestation", "",
               "I confirm the access log is reviewed monthly",
               "and exceptions are reported to the board.", "",
               "Signed: Information Governance Lead", "Date: 2026-09-12"]

WHITEBOARD = ["# Ward 4 board - Tuesday", "",
              "Bed 3  discharge today  referral sent",
              "Bed 7  awaiting pharmacy",
              "Bed 9  readmission risk HIGH - see coordinator"]

#: What each picture says, page by page: what a perfect OCR would read.
DRAWN = {
    "referral-sla-scanned.pdf": REFERRAL_SLA,
    "audit-letter-mixed.pdf": [None, ATTESTATION],
    "ward-whiteboard.png": [WHITEBOARD],
}


def audit_letter_mixed():
    """Page 1 typed, page 2 a signed page that was scanned back in."""
    typed = _text_pdf([
        "Audit response - CarePath access logging",
        "Document GOV-CAREPATH-014  Version 1.0  Owner: Information Governance Lead",
        "Date: 2026-09-12",
        "",
        "The 2025 audits found the network could not list who opened a record and why.",
        "CarePath records every access with a stated reason (BR-05, REG-01).",
        "The signed attestation is on the next page.",
    ])
    scanned = BytesIO()
    page_image(ATTESTATION).save(scanned, format="PDF", resolution=200)
    writer = PdfWriter()
    for source in (typed, scanned.getvalue()):
        for page in PdfReader(BytesIO(source)).pages:
            writer.add_page(page)
    with open(OUT / "audit-letter-mixed.pdf", "wb") as target:
        writer.write(target)


def whiteboard():
    page_image(WHITEBOARD, size=(1600, 900)).save(OUT / "ward-whiteboard.png")


# ------------------------------------------------------------------ text

ON_CALL = """On-call handover notes, week 38
Monday was quiet until the referral queue backed up around 14:00.
The coordinator dashboard showed 41 open referrals, most from Northvale.
Restarting the worker cleared the queue but nobody knows why it stalled.
Owner for the follow-up is TBD.
Tuesday the nightly export ran long again and finished at 06:40.
The risk scores were stale for the morning huddle.
We agreed to look at the export schedule but the new time is TBD.
Wednesday there was a short outage of the patient search page.
It matched the pattern from the August incident but recovered by itself.
Somebody should check whether the connection pool fix is deployed. TODO.
Thursday was uneventful apart from two password resets.
Friday the clinic in Eastfield reported slow page loads after 16:00.
The network team said there was maintenance on their side.
Saturday and Sunday had no pages.
Next week we expect the Northvale data feed change to go live.
The rollback plan for that change is to be confirmed.
Please read the deployment runbook before Thursday.
Questions go to the platform channel.
"""

TEMPLATE = """# Discharge summary

Patient:
Ward:
Summary: TBD
"""

ESCALATION = """# Escalation policy - CarePath referrals

Document: POL-CAREPATH-021 · Version 1.1 · Status: Approved · Owner: Care Director · 2026-07-30

Demonstration artifact. Synthetic content only - not a clinical record.

## When to escalate

| Situation | Escalate to | Within | Answers |
| --- | --- | --- | --- |
| Urgent referral not acknowledged | Care coordinator | 4 hours | BR-02 |
| Consent missing before export | Information Governance | Same day | BR-05 |
| Patient search unavailable | Platform on-call | 15 minutes | NFR-06 |
| Northvale feed rejected | Integration lead | 1 working day | BR-99 |

## Who decides

The care coordinator decides whether a referral is clinically urgent. The platform
on-call engineer decides whether an outage is declared; the incident process in the
operations runbook applies from then on.
"""

MENU = """# Staff canteen menu

| Dish | Price | Allergens |
| --- | --- | --- |
| Roast chicken with potatoes | 4.20 | none |
| Mushroom risotto | 3.80 | dairy |
| Beef lasagne | 4.50 | gluten, dairy |
| Halloumi wrap | 3.60 | gluten, dairy |
| Lamb curry with rice | 4.40 | none |
| Apple crumble and custard | 1.90 | gluten, dairy, egg |

Soup is served with fresh bread. Bring your own cup for a discount on coffee.
"""


def text_files():
    (OUT / "on-call-handover.txt").write_text(ON_CALL, encoding="utf-8")
    (OUT / "discharge-summary-template.md").write_text(TEMPLATE, encoding="utf-8")
    (OUT / "escalation-policy.md").write_text(ESCALATION, encoding="utf-8")
    (OUT / "escalation-policy-copy.md").write_text(ESCALATION, encoding="utf-8")
    (OUT / "staff-canteen-menu.md").write_text(MENU, encoding="utf-8")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    discharge_checklist()
    bed_capacity()
    referral_sla_scanned()
    audit_letter_mixed()
    whiteboard()
    text_files()
    print("Built the samples in", OUT)
