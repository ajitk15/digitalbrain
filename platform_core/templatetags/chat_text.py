"""Small safe presentation subset: source/provider HTML is always escaped."""

import re

from django import template
from django.utils.html import escape
from django.utils.safestring import mark_safe

register = template.Library()


def inline(value):
    value = str(escape(value))
    value = re.sub(r"`([^`]+)`", r"<code>\1</code>", value)
    return re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", value)


@register.filter
def chat_text(value):
    blocks = []
    paragraph = []
    code = []
    in_code = False
    in_quote = False
    list_kind = None

    def flush():
        if paragraph:
            blocks.append("<p>" + "<br>".join(inline(line) for line in paragraph) + "</p>")
            paragraph.clear()

    lines = str(value).splitlines()
    skip_until = 0
    for index, line in enumerate(lines):
        if index < skip_until:
            continue
        if (
            not in_code
            and "|" in line
            and index + 1 < len(lines)
            and re.fullmatch(r"\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*", lines[index + 1])
        ):
            flush()
            if list_kind:
                blocks.append(f"</{list_kind}>")
                list_kind = None

            def cells(row):
                return row.strip().strip("|").split("|")

            blocks.append('<div class="chat-table"><table><thead><tr>')
            blocks.extend("<th>" + inline(cell.strip()) + "</th>" for cell in cells(line))
            blocks.append("</tr></thead><tbody>")
            skip_until = index + 2
            while (
                skip_until < len(lines) and "|" in lines[skip_until] and lines[skip_until].strip()
            ):
                blocks.append("<tr>")
                blocks.extend(
                    "<td>" + inline(cell.strip()) + "</td>" for cell in cells(lines[skip_until])
                )
                blocks.append("</tr>")
                skip_until += 1
            blocks.append("</tbody></table></div>")
            continue
        if line.strip().startswith("```"):
            flush()
            if list_kind:
                blocks.append(f"</{list_kind}>")
                list_kind = None
            if in_code:
                blocks.append("<pre><code>" + str(escape("\n".join(code))) + "</code></pre>")
                code = []
            in_code = not in_code
            continue
        if in_code:
            code.append(line)
            continue
        quote = re.match(r"^\s*> ?(.*)", line)
        if quote and not in_code:
            if list_kind:
                blocks.append(f"</{list_kind}>")
                list_kind = None
            flush()
            if not in_quote:
                blocks.append('<blockquote class="chat-note">')
                in_quote = True
            blocks.append("<p>" + inline(quote[1]) + "</p>")
            continue
        if in_quote:
            blocks.append("</blockquote>")
            in_quote = False
        item = re.match(r"^\s*(?:[-*] |\d+\. )(.*)", line)
        kind = "ol" if re.match(r"^\s*\d+\. ", line) else "ul"
        if list_kind and (not item or kind != list_kind):
            blocks.append(f"</{list_kind}>")
            list_kind = None
        if item:
            flush()
            if not list_kind:
                blocks.append(f"<{kind}>")
                list_kind = kind
            blocks.append("<li>" + inline(item[1]) + "</li>")
        elif not line.strip():
            flush()
        elif re.match(r"^#{1,6} ", line):
            flush()
            # h1/h2 both become h3: the page already owns h1 and h2, so an answer
            # must not outrank its own container.
            depth = len(line) - len(line.lstrip("#"))
            tag = "h3" if depth <= 2 else ("h4" if depth == 3 else "h5")
            blocks.append(f"<{tag}>" + inline(re.sub(r"^#{1,6} ", "", line)) + f"</{tag}>")
        else:
            paragraph.append(line)
    flush()
    if in_quote:
        blocks.append("</blockquote>")
    if list_kind:
        blocks.append(f"</{list_kind}>")
    if in_code:
        blocks.append("<pre><code>" + str(escape("\n".join(code))) + "</code></pre>")
    return mark_safe("".join(blocks))


@register.filter
def source_chips(citations):
    """Group verified citations by source document.

    One document cited five times is one chip carrying [1,2,3,4,5], not five
    identical rows. The numbers are the inline [n] markers in the answer, so the
    grouping never breaks the link between a claim and its evidence.

    The digest is deliberately dropped: it is a server-side integrity token used
    to verify a citation against its source, and has no business in the browser.
    """
    chips = {}
    for index, citation in enumerate(citations or [], 1):
        key = citation.get("id")
        chip = chips.setdefault(
            key,
            {
                "id": key,
                "title": citation.get("title", ""),
                "graph_version": citation.get("graph_version"),
                "numbers": [],
                "excerpts": [],
            },
        )
        chip["numbers"].append(index)
        chip["excerpts"].append({"number": index, "text": citation.get("excerpt", "")})
    return list(chips.values())
