"""Renderers, one per output format.

Each takes the blocks from `mdparse` and writes one file. They share nothing but
the block list, because the formats have genuinely different shapes: a
spreadsheet wants the tables and little else, a deck wants headings and the
first few bullets under each, and Word wants the whole document.
"""

import csv
import html as htmllib
from pathlib import Path

from mdparse import Block, front_matter, plain, spans, title_of

# --------------------------------------------------------------------- Word


def write_docx(blocks: list[Block], target: Path, fallback: str) -> None:
    """A Word document. Headings map to Word's own heading styles."""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt

    document = Document()
    title = title_of(blocks, fallback)
    document.core_properties.title = title
    meta = front_matter(blocks)
    document.core_properties.author = meta.get("Owner", "CarePath programme")
    document.core_properties.comments = (
        "Demonstration artifact. Synthetic content only - not a clinical record."
    )

    seen_title = False
    skip_meta = True
    for block in blocks:
        if block.kind == "heading":
            if block.level == 1 and not seen_title:
                seen_title = True
                document.add_heading(plain(block.text), 0)
                if meta:
                    line = document.add_paragraph()
                    line.alignment = WD_ALIGN_PARAGRAPH.LEFT
                    run = line.add_run(
                        " · ".join(f"{key}: {value}" for key, value in meta.items())
                    )
                    run.italic = True
                    run.font.size = Pt(9)
                continue
            skip_meta = False
            document.add_heading(plain(block.text), min(block.level, 4))

        elif block.kind == "table":
            if skip_meta and not any(block.rows[0]):
                skip_meta = False
                continue
            _docx_table(document, block)

        elif block.kind == "para":
            paragraph = document.add_paragraph()
            _docx_runs(paragraph, block.text)

        elif block.kind == "list":
            for item in block.items:
                style = "List Number" if block.ordered else "List Bullet"
                paragraph = document.add_paragraph(style=style)
                _docx_runs(paragraph, item)

        elif block.kind == "code":
            paragraph = document.add_paragraph()
            run = paragraph.add_run(block.text)
            run.font.name = "Consolas"
            run.font.size = Pt(9)

        elif block.kind == "quote":
            paragraph = document.add_paragraph(style="Intense Quote")
            _docx_runs(paragraph, block.text)

    document.save(target)


def _docx_runs(paragraph, text: str) -> None:
    for style, piece in spans(text):
        if not piece:
            continue
        run = paragraph.add_run(piece.replace("[ ]", "☐").replace("[x]", "☑"))
        run.bold = style == "b"
        run.italic = style == "i"
        if style == "c":
            run.font.name = "Consolas"


def _docx_table(document, block: Block) -> None:
    from docx.shared import Pt

    width = max(len(row) for row in block.rows)
    table = document.add_table(rows=0, cols=width)
    table.style = "Light Grid Accent 1"
    for index, row in enumerate(block.rows):
        cells = table.add_row().cells
        for position in range(width):
            value = plain(row[position]) if position < len(row) else ""
            cells[position].text = value.replace("[ ]", "☐").replace("[x]", "☑")
            for paragraph in cells[position].paragraphs:
                for run in paragraph.runs:
                    run.font.size = Pt(9)
                    run.bold = index == 0
    document.add_paragraph()


# ---------------------------------------------------------------- Spreadsheet


def write_xlsx(blocks: list[Block], target: Path, fallback: str) -> None:
    """A workbook: every table becomes a sheet, the prose becomes a first sheet.

    This is the format where the conversion is a genuine change of shape rather
    than a change of container. A traceability matrix or a risk register is
    *born* as a grid, and giving it back as one is the point.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    workbook = Workbook()
    header = Font(bold=True, color="FFFFFF")
    fill = PatternFill("solid", fgColor="1F4E79")

    def head_row(sheet, labels: list[str]) -> None:
        for column, label in enumerate(labels, start=1):
            cell = sheet.cell(row=1, column=column, value=label)
            cell.font = header
            cell.fill = fill

    # Every sheet keeps all its columns populated. A sheet with gaps converts to
    # Markdown as a grid of "NaN", because the converter reads it through a
    # dataframe - and a citation quoting NaN is a citation nobody trusts.
    overview = workbook.active
    overview.title = "Overview"
    head_row(overview, ["Field", "Value"])
    row = 2
    overview.cell(row=row, column=1, value="Title").font = Font(bold=True)
    overview.cell(row=row, column=2, value=title_of(blocks, fallback))
    row += 1
    for key, value in front_matter(blocks).items():
        overview.cell(row=row, column=1, value=key).font = Font(bold=True)
        overview.cell(row=row, column=2, value=value)
        row += 1
    overview.cell(row=row, column=1, value="Notice").font = Font(bold=True)
    overview.cell(
        row=row,
        column=2,
        value="Demonstration artifact. Synthetic content only - not a clinical record.",
    )
    overview.column_dimensions["A"].width = 26
    overview.column_dimensions["B"].width = 96
    overview.freeze_panes = "A2"

    narrative = workbook.create_sheet("Narrative")
    head_row(narrative, ["Section", "Text"])
    row = 2
    current = title_of(blocks, fallback)
    for block in blocks:
        if block.kind == "heading" and block.level > 1:
            current = plain(block.text)
        elif block.kind in {"para", "quote"}:
            narrative.cell(row=row, column=1, value=current)
            narrative.cell(row=row, column=2, value=plain(block.text)).alignment = Alignment(
                wrap_text=True, vertical="top"
            )
            row += 1
        elif block.kind == "list":
            for item in block.items:
                narrative.cell(row=row, column=1, value=current)
                narrative.cell(row=row, column=2, value="• " + plain(item))
                row += 1
    narrative.column_dimensions["A"].width = 34
    narrative.column_dimensions["B"].width = 110
    narrative.freeze_panes = "A2"

    names: set[str] = set()
    heading = fallback
    index = 0
    for block in blocks:
        if block.kind == "heading":
            heading = plain(block.text)
            continue
        if block.kind != "table" or len(block.rows) < 2:
            continue
        if not any(block.rows[0]):  # the metadata table
            continue
        index += 1
        sheet = workbook.create_sheet(_sheet_name(heading, index, names))
        span = max(len(row) for row in block.rows)
        for position, source_row in enumerate(block.rows, start=1):
            # Padded to the full width. A ragged row leaves trailing blanks,
            # which the converter renders as NaN.
            row_values = list(source_row) + [""] * (span - len(source_row))
            for column, value in enumerate(row_values, start=1):
                cell = sheet.cell(row=position, column=column, value=plain(value) or "—")
                cell.alignment = Alignment(wrap_text=True, vertical="top")
                if position == 1:
                    cell.font = header
                    cell.fill = fill
        sheet.freeze_panes = "A2"
        for column in range(1, max(len(row) for row in block.rows) + 1):
            longest = max(
                (len(plain(row[column - 1])) for row in block.rows if column <= len(row)),
                default=10,
            )
            sheet.column_dimensions[get_column_letter(column)].width = min(60, max(12, longest + 2))
    workbook.save(target)


def _sheet_name(heading: str, index: int, seen: set[str]) -> str:
    cleaned = "".join(char for char in heading if char.isalnum() or char in " -_")[:24].strip()
    name = f"{index}. {cleaned or 'Table'}"[:31]
    while name in seen:
        index += 1
        name = f"{index}. {cleaned or 'Table'}"[:31]
    seen.add(name)
    return name


# ---------------------------------------------------------------------- HTML

STYLE = """
:root { color-scheme: light dark; }
body { font: 16px/1.65 -apple-system, "Segoe UI", system-ui, sans-serif;
       max-width: 62rem; margin: 0 auto; padding: 2rem 1rem 6rem; color: #16191d;
       background: #fff; }
h1 { font-size: 2rem; line-height: 1.2; border-bottom: 3px solid #1f4e79;
     padding-bottom: .4rem; }
h2 { font-size: 1.35rem; margin-top: 2.4rem; color: #1f4e79; }
h3 { font-size: 1.1rem; margin-top: 1.8rem; }
table { border-collapse: collapse; width: 100%; margin: 1.2rem 0; font-size: .92rem; }
th, td { border: 1px solid #d3d8de; padding: .5rem .65rem; text-align: left;
         vertical-align: top; }
th { background: #eef2f6; font-weight: 600; }
tr:nth-child(even) td { background: #fafbfc; }
code { font: .88em ui-monospace, Consolas, monospace; background: #f1f3f5;
       padding: .1em .35em; border-radius: 3px; }
pre { background: #f6f8fa; padding: 1rem; overflow-x: auto; border-radius: 6px;
      border: 1px solid #e3e7ec; }
pre code { background: none; padding: 0; }
blockquote { margin: 1.4rem 0; padding: .8rem 1.1rem; border-left: 4px solid #c9a227;
             background: #fdf9ec; }
.meta { font-size: .85rem; color: #5b6470; margin: -.4rem 0 2rem; }
.notice { border: 1px solid #c9a227; background: #fdf9ec; padding: .9rem 1.1rem;
          border-radius: 6px; font-size: .9rem; margin-bottom: 2rem; }
hr { border: none; border-top: 1px solid #e3e7ec; margin: 2.5rem 0; }
@media (prefers-color-scheme: dark) {
  body { background: #14171a; color: #e6e9ec; }
  th { background: #1e2429; } tr:nth-child(even) td { background: #181c20; }
  th, td { border-color: #2d353d; }
  h2 { color: #7fb3e0; } h1 { border-bottom-color: #7fb3e0; }
  code, pre { background: #1b2025; } pre { border-color: #2d353d; }
  blockquote, .notice { background: #241f10; }
}
"""


def write_html(blocks: list[Block], target: Path, fallback: str) -> None:
    """A self-contained page. No external stylesheet, no script."""
    title = title_of(blocks, fallback)
    out = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{htmllib.escape(title)}</title>",
        f"<style>{STYLE}</style></head><body>",
    ]
    meta = front_matter(blocks)
    seen_title = False
    skip_meta = True
    for block in blocks:
        if block.kind == "heading":
            if block.level == 1 and not seen_title:
                seen_title = True
                out.append(f"<h1>{htmllib.escape(plain(block.text))}</h1>")
                if meta:
                    pairs = " · ".join(
                        f"<strong>{htmllib.escape(k)}</strong> {htmllib.escape(v)}"
                        for k, v in meta.items()
                    )
                    out.append(f'<p class="meta">{pairs}</p>')
                out.append(
                    '<p class="notice">Demonstration artifact. Synthetic content only — '
                    "not a clinical record and not a medical device.</p>"
                )
                continue
            skip_meta = False
            level = min(block.level, 6)
            out.append(f"<h{level}>{_inline(block.text)}</h{level}>")
        elif block.kind == "table":
            if skip_meta and not any(block.rows[0]):
                skip_meta = False
                continue
            out.append(_html_table(block))
        elif block.kind == "para":
            out.append(f"<p>{_inline(block.text)}</p>")
        elif block.kind == "list":
            tag = "ol" if block.ordered else "ul"
            items = "".join(f"<li>{_inline(item)}</li>" for item in block.items)
            out.append(f"<{tag}>{items}</{tag}>")
        elif block.kind == "code":
            out.append(f"<pre><code>{htmllib.escape(block.text)}</code></pre>")
        elif block.kind == "quote":
            out.append(f"<blockquote>{_inline(block.text)}</blockquote>")
        elif block.kind == "rule":
            out.append("<hr>")
    out.append("</body></html>")
    target.write_text("\n".join(out), encoding="utf-8")


def _inline(text: str) -> str:
    pieces = []
    for style, piece in spans(text):
        escaped = htmllib.escape(piece)
        if style == "b":
            pieces.append(f"<strong>{escaped}</strong>")
        elif style == "i":
            pieces.append(f"<em>{escaped}</em>")
        elif style == "c":
            pieces.append(f"<code>{escaped}</code>")
        else:
            pieces.append(escaped)
    return "".join(pieces)


def _html_table(block: Block) -> str:
    head = "".join(f"<th>{_inline(cell)}</th>" for cell in block.rows[0])
    body = "".join(
        "<tr>" + "".join(f"<td>{_inline(cell)}</td>" for cell in row) + "</tr>"
        for row in block.rows[1:]
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


# ----------------------------------------------------------------- Plain text


def write_txt(blocks: list[Block], target: Path, fallback: str) -> None:
    """Plain text, laid out with rules and indentation instead of markup."""
    lines: list[str] = []
    title = title_of(blocks, fallback)
    lines += ["=" * 78, title.upper().center(78), "=" * 78, ""]
    meta = front_matter(blocks)
    for key, value in meta.items():
        lines.append(f"  {key + ':':<22}{value}")
    if meta:
        lines.append("")
    lines += [
        "  DEMONSTRATION ARTIFACT - synthetic content only, not a clinical record.",
        "",
    ]
    skip_meta = True
    seen_title = False
    for block in blocks:
        if block.kind == "heading":
            if block.level == 1 and not seen_title:
                seen_title = True
                continue
            skip_meta = False
            text = plain(block.text)
            if block.level == 2:
                lines += ["", text.upper(), "-" * len(text), ""]
            else:
                lines += ["", f"{'  ' * (block.level - 2)}{text}", ""]
        elif block.kind == "table":
            if skip_meta and not any(block.rows[0]):
                skip_meta = False
                continue
            lines += _txt_table(block) + [""]
        elif block.kind == "para":
            lines += _wrap(plain(block.text), 78) + [""]
        elif block.kind == "list":
            for index, item in enumerate(block.items, start=1):
                marker = f"{index}." if block.ordered else "*"
                wrapped = _wrap(plain(item), 74)
                lines.append(f"  {marker} {wrapped[0]}")
                lines += [f"     {line}" for line in wrapped[1:]]
            lines.append("")
        elif block.kind == "code":
            lines += [f"    {line}" for line in block.text.split("\n")] + [""]
        elif block.kind == "quote":
            lines += [f"  | {line}" for line in _wrap(plain(block.text), 74)] + [""]
        elif block.kind == "rule":
            lines += ["-" * 78, ""]
    target.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _wrap(text: str, width: int) -> list[str]:
    words = text.split()
    if not words:
        return [""]
    out, line = [], words[0]
    for word in words[1:]:
        if len(line) + 1 + len(word) <= width:
            line += " " + word
        else:
            out.append(line)
            line = word
    out.append(line)
    return out


def _txt_table(block: Block) -> list[str]:
    rows = [[plain(cell) for cell in row] for row in block.rows]
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    widths = [
        min(34, max(len(row[column]) for row in rows)) for column in range(width)
    ]
    out = []
    for index, row in enumerate(rows):
        out.append("  " + " | ".join(cell[: widths[i]].ljust(widths[i]) for i, cell in enumerate(row)))
        if index == 0:
            out.append("  " + "-+-".join("-" * w for w in widths))
    return out


# ------------------------------------------------------------------------ CSV


def write_csv(rows: list[list[str]], target: Path) -> None:
    """A CSV, written with CRLF the way an export from a tracker arrives."""
    with target.open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerows(rows)


# ------------------------------------------------------------------------ PDF


def write_pdf(blocks: list[Block], target: Path, fallback: str) -> None:
    """A PDF with real text - not an image - so it converts without OCR."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        HRFlowable,
        ListFlowable,
        ListItem,
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    styles = getSampleStyleSheet()
    body = ParagraphStyle(
        "body", parent=styles["BodyText"], fontSize=9.5, leading=13.5, alignment=TA_LEFT,
        spaceAfter=6,
    )
    cell = ParagraphStyle("cell", parent=body, fontSize=8, leading=10.5, spaceAfter=0)
    head = ParagraphStyle("cellhead", parent=cell, textColor=colors.white, fontName="Helvetica-Bold")
    meta_style = ParagraphStyle(
        "meta", parent=body, fontSize=8.5, textColor=colors.HexColor("#5b6470")
    )

    title = title_of(blocks, fallback)
    document = SimpleDocTemplate(
        str(target), pagesize=A4,
        leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm, bottomMargin=18 * mm,
        title=title, author="CarePath programme",
        subject="Demonstration artifact - synthetic content only",
    )

    story: list = []
    meta = front_matter(blocks)
    seen_title = False
    skip_meta = True
    for block in blocks:
        if block.kind == "heading":
            if block.level == 1 and not seen_title:
                seen_title = True
                story.append(Paragraph(_pdf_inline(block.text), styles["Title"]))
                if meta:
                    story.append(
                        Paragraph(
                            " &nbsp;·&nbsp; ".join(
                                f"<b>{k}</b> {v}" for k, v in meta.items()
                            ),
                            meta_style,
                        )
                    )
                story.append(
                    Paragraph(
                        "<i>Demonstration artifact. Synthetic content only — not a clinical "
                        "record and not a medical device.</i>",
                        meta_style,
                    )
                )
                story.append(Spacer(1, 6 * mm))
                continue
            skip_meta = False
            style = styles["Heading2"] if block.level == 2 else styles["Heading3"]
            story.append(Spacer(1, 3 * mm))
            story.append(Paragraph(_pdf_inline(block.text), style))
        elif block.kind == "table":
            if skip_meta and not any(block.rows[0]):
                skip_meta = False
                continue
            story.append(_pdf_table(block, cell, head, Table, TableStyle, colors, mm))
            story.append(Spacer(1, 3 * mm))
        elif block.kind == "para":
            story.append(Paragraph(_pdf_inline(block.text), body))
        elif block.kind == "list":
            story.append(
                ListFlowable(
                    [ListItem(Paragraph(_pdf_inline(item), body)) for item in block.items],
                    bulletType="1" if block.ordered else "bullet",
                    leftIndent=12,
                )
            )
            story.append(Spacer(1, 2 * mm))
        elif block.kind == "code":
            story.append(
                Paragraph(
                    f'<font face="Courier" size="8">{_escape(block.text)}</font>'.replace(
                        "\n", "<br/>"
                    ),
                    body,
                )
            )
        elif block.kind == "quote":
            story.append(Paragraph(f"<i>{_pdf_inline(block.text)}</i>", body))
        elif block.kind == "rule":
            story.append(HRFlowable(width="100%", color=colors.HexColor("#d3d8de")))
            story.append(Spacer(1, 3 * mm))
    if not story:
        story.append(PageBreak())
    document.build(story)


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _pdf_inline(text: str) -> str:
    out = []
    for style, piece in spans(text):
        escaped = _escape(piece)
        if style == "b":
            out.append(f"<b>{escaped}</b>")
        elif style == "i":
            out.append(f"<i>{escaped}</i>")
        elif style == "c":
            out.append(f'<font face="Courier">{escaped}</font>')
        else:
            out.append(escaped)
    return "".join(out)


def _pdf_table(block, cell, head, Table, TableStyle, colors, mm):
    from reportlab.platypus import Paragraph

    width = max(len(row) for row in block.rows)
    data = [
        [
            Paragraph(_pdf_inline(row[i]) if i < len(row) else "", head if n == 0 else cell)
            for i in range(width)
        ]
        for n, row in enumerate(block.rows)
    ]
    available = 170 * mm
    table = Table(data, colWidths=[available / width] * width, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F4E79")),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c8ced5")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f6f8fa")]),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    return table
