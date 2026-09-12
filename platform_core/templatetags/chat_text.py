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
            blocks.append("<h3>" + inline(re.sub(r"^#{1,6} ", "", line)) + "</h3>")
        else:
            paragraph.append(line)
    flush()
    if list_kind:
        blocks.append(f"</{list_kind}>")
    if in_code:
        blocks.append("<pre><code>" + str(escape("\n".join(code))) + "</code></pre>")
    return mark_safe("".join(blocks))
