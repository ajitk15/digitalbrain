"""A small Markdown reader, sufficient for the documents in this project.

Not a general Markdown implementation. It understands exactly the constructs the
CarePath documents use - headings, paragraphs, pipe tables, bullet and numbered
lists, fenced code, block quotes and horizontal rules - and it turns them into a
list of blocks that each renderer walks.

A real converter was avoided deliberately: pandoc is another dependency to
install on a demonstration laptop, and the documents are ours, so the subset
they use is known rather than guessed at.
"""

import re
from dataclasses import dataclass, field


@dataclass
class Block:
    """One structural element of a document."""

    kind: str  # heading | para | table | list | code | quote | rule
    level: int = 0
    text: str = ""
    rows: list[list[str]] = field(default_factory=list)
    items: list[str] = field(default_factory=list)
    ordered: bool = False


#: `**bold**`, `*italic*`, `` `code` `` and `[text](target)`.
EMPHASIS = re.compile(r"\*\*(.+?)\*\*|\*(.+?)\*|`(.+?)`|\[(.+?)\]\((.+?)\)")


def plain(text: str) -> str:
    """Strip inline markup, keeping the words.

    Renderers that cannot carry emphasis - a spreadsheet cell, a plain-text
    file - use this rather than emitting asterisks that a reader would have to
    mentally delete.
    """

    def replace(match: re.Match[str]) -> str:
        bold, italic, code, link, _target = match.groups()
        return bold or italic or code or link or ""

    return EMPHASIS.sub(replace, text)


def spans(text: str) -> list[tuple[str, str]]:
    """Split a line into (style, text) pairs, style being one of b, i, c or "".

    Renderers that *can* carry emphasis walk this instead of `plain`, so a
    requirement that the source emphasised stays emphasised in Word.
    """
    out: list[tuple[str, str]] = []
    position = 0
    for match in EMPHASIS.finditer(text):
        if match.start() > position:
            out.append(("", text[position : match.start()]))
        bold, italic, code, link, _target = match.groups()
        if bold:
            out.append(("b", bold))
        elif italic:
            out.append(("i", italic))
        elif code:
            out.append(("c", code))
        else:
            out.append(("", link or ""))
        position = match.end()
    if position < len(text):
        out.append(("", text[position:]))
    return out or [("", text)]


def _table_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _is_divider(line: str) -> bool:
    """True for a `| --- | --- |` separator.

    The dash is required. Without it `| | |` - the empty header row these
    documents use to open a metadata table - reads as a separator, the real
    first row is mistaken for the header, and the metadata table stops being
    recognisable as one.
    """
    stripped = line.strip()
    if not stripped.startswith("|") or "-" not in stripped:
        return False
    return set(stripped) <= set("|-: ")


def parse(text: str) -> list[Block]:
    """Read Markdown into blocks."""
    blocks: list[Block] = []
    lines = text.replace("\r\n", "\n").split("\n")
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        if not stripped:
            index += 1
            continue

        if stripped.startswith("```"):
            index += 1
            body: list[str] = []
            while index < len(lines) and not lines[index].strip().startswith("```"):
                body.append(lines[index])
                index += 1
            index += 1
            blocks.append(Block("code", text="\n".join(body)))
            continue

        if set(stripped) <= {"-", "*", "_"} and len(stripped) >= 3:
            blocks.append(Block("rule"))
            index += 1
            continue

        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            blocks.append(Block("heading", level=level, text=stripped[level:].strip()))
            index += 1
            continue

        if stripped.startswith(">"):
            quoted: list[str] = []
            while index < len(lines) and lines[index].strip().startswith(">"):
                quoted.append(lines[index].strip().lstrip(">").strip())
                index += 1
            blocks.append(Block("quote", text=" ".join(part for part in quoted if part)))
            continue

        if stripped.startswith("|"):
            rows: list[list[str]] = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                if not _is_divider(lines[index]):
                    rows.append(_table_row(lines[index]))
                index += 1
            if rows:
                blocks.append(Block("table", rows=rows))
            continue

        bullet = re.match(r"^\s*[-*+]\s+(.*)$", line)
        number = re.match(r"^\s*\d+[.)]\s+(.*)$", line)
        if bullet or number:
            ordered = number is not None
            items: list[str] = []
            while index < len(lines):
                candidate = lines[index]
                nxt_bullet = re.match(r"^\s*[-*+]\s+(.*)$", candidate)
                nxt_number = re.match(r"^\s*\d+[.)]\s+(.*)$", candidate)
                if nxt_bullet and not ordered:
                    items.append(nxt_bullet.group(1).strip())
                elif nxt_number and ordered:
                    items.append(nxt_number.group(1).strip())
                elif candidate.strip() and candidate.startswith(("  ", "\t")) and items:
                    items[-1] += " " + candidate.strip()
                else:
                    break
                index += 1
            blocks.append(Block("list", items=items, ordered=ordered))
            continue

        paragraph: list[str] = []
        while index < len(lines):
            candidate = lines[index]
            if not candidate.strip() or candidate.strip().startswith(("#", "|", ">", "```")):
                break
            if re.match(r"^\s*([-*+]|\d+[.)])\s+", candidate):
                break
            paragraph.append(candidate.strip())
            index += 1
        blocks.append(Block("para", text=" ".join(paragraph)))
    return blocks


def title_of(blocks: list[Block], fallback: str) -> str:
    """The first level-one heading, or `fallback`."""
    for block in blocks:
        if block.kind == "heading" and block.level == 1:
            return plain(block.text)
    return fallback


def front_matter(blocks: list[Block]) -> dict[str, str]:
    """Read the two-column metadata table these documents open with.

    The convention is a table whose header row is empty - `| | |` - immediately
    after the title. Returning it separately lets a renderer put the metadata
    somewhere appropriate for its format: a Word properties block, a slide
    footer, a spreadsheet's first sheet.
    """
    for block in blocks[:3]:
        if block.kind == "table" and len(block.rows) > 1 and not any(block.rows[0]):
            return {
                plain(row[0]).strip(): plain(row[1]).strip()
                for row in block.rows[1:]
                if len(row) >= 2 and row[0]
            }
    return {}
