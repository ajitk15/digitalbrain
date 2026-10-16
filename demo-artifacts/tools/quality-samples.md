# CarePath source-quality samples

Ten CarePath documents in `../docs/08-quality-samples/`, nearly all with one deliberate
problem. Use them to check by hand:

- the **Source check** card, on each document page and Knowledge source;
- the pills in the Sources list;
- **Check sources**, the source map.

They upload with the rest of `../docs/`.

The scorecard reads them too, so the baseline was reset with them in.
`on-call-handover.txt` mentions "the August incident" in an unstructured line, which
pushes the postmortem out of the graph's citations for "Why did the service fail on
14 August?". That took graph found from 16 to 15 and MRR from 0.716 to 0.701. A
noisy document costing retrieval is the point of the sample. The keyword path still
ranks the postmortem first.

With OCR off, the scanned PDF and the whiteboard are refused, as an upload would be.
The scorecard skips them, and only them. A refusal anywhere else in `../docs/` still
fails it.

Rebuild them with:

```bash
.venv/Scripts/python.exe demo-artifacts/tools/build_quality_samples.py
```

`build_formats.py` leaves that folder alone when it rebuilds `../docs/`. The build
uses only what the platform already installs (Pillow, openpyxl, pypdf). The `.docx`
and the typed page of the mixed PDF are written by hand.

## What each one should show

| File | Planted problem | Source check |
| --- | --- | --- |
| `escalation-policy.md` | None. A clean control: document line, headings, a proper table. It cites BR-02, BR-05 and NFR-06, plus **BR-99, which nothing defines**. | Ready 100 |
| `escalation-policy-copy.md` | A byte-for-byte copy of the policy. | Ready 100. The source map lists the pair as an identical duplicate. |
| `discharge-checklist.docx` | Two Word tables, neither marked as a header row. The first has a bold first row, which the parser promotes to the header. The second has a plain first row. | Needs work 87: one table has a blank header, so the graph calls its columns "column 1", "column 2" |
| `bed-capacity-export.xlsx` | A raw export: no column names, rows repeated, cells empty, no title. | Needs work 60: placeholder column names, repeated rows, and no document number, owner or date |
| `on-call-handover.txt` | 19 lines with no headings, and four TBD or TODO notes. | Needs work 63: no structure, open placeholders, opening names its owner only |
| `discharge-summary-template.md` | An unfilled template. | Needs work 58: almost no text, an open TBD, nothing says what it is |
| `staff-canteen-menu.md` | Not a CarePath document at all. | Needs work 85 (no document line). The source map flags it **off-topic** (similarity 0.01) and isolated. |
| `referral-sla-scanned.pdf` | Two pages that are pictures of text, with no text layer. | OCR off: **refused**, and says it is a scan - none of its 2 pages has a text layer - and how OCR is turned on. OCR on: Ready, two pages read by OCR, citing BR-02, NFR-06 and A-02. |
| `audit-letter-mixed.pdf` | Page 1 is typed. Page 2 is a signed attestation that was scanned back in. | OCR off: Needs work 80, because page 2 of 2 had no text. OCR on: Ready, with page 2 read by OCR. |
| `ward-whiteboard.png` | A photo of a ward board. | OCR off: refused, because images need OCR. OCR on: read, but Needs work if Tesseract's confidence is under 60% (the score is a whiteboard's typical one). |

These are the results with the whole of `../docs/` uploaded, 27 documents plus these:

- **Check sources** should report 31 of 35 connected with OCR off, or 32 of 37 with
  OCR on.
- **BR-99** should be listed among the references nothing defines, next to the corpus's
  own gaps (A-01 to A-03, F-01 to F-04, INC-2026-0814).
- The bed export, the template, the handover notes and the menu should be isolated,
  and with OCR on the whiteboard too. The menu is also flagged off-topic.

The exact scores above were measured with `source_quality.assess` and
`source_map.analyse`. The "OCR on" rows used a stand-in reader that returns what was
drawn on each page, with a confidence of 88, or 41 for the whiteboard. With real
Tesseract (`ocr = true` in the config), the text and confidence will be whatever
Tesseract reads.
