"""Sorting a table by its column headings, the same way on every page.

A heading is a plain link (`?sort=<column>&dir=asc|desc`) rather than a script,
so a table sorts with JavaScript off, and the link keeps whatever else the page
was asked for - filters, a search - and starts again at page one. The heading
that sorts the table says so with an arrow and `aria-sort`; clicking it again
turns the order round. `templates/_sort_header.html` draws one.

A column sorts either a list already in memory (`value`, a function of a row)
or a queryset (`field`, a model field path). Either way a row with nothing to
sort on goes last, whichever way the column sorts: a blank priority is not the
most urgent one.
"""

import re
from dataclasses import dataclass
from urllib.parse import urlencode

from django.db.models import F

DIRECTIONS = {"asc": "ascending", "desc": "descending"}
#: Priority words in order of urgency, Jira's and ServiceNow's together.
#: ServiceNow also numbers them ("2 - High"), and the number wins.
PRIORITY_RANK = {"blocker": 0, "highest": 1, "critical": 1, "high": 2, "medium": 3,
                 "moderate": 3, "low": 4, "lowest": 5, "planning": 5, "trivial": 5}
NUMBERED = re.compile(r"^(.*?)(\d+)$")


@dataclass(frozen=True)
class Column:
    label: str
    #: The way the first click sorts: names and keys A to Z, dates and amounts
    #: newest or largest first.
    first: str = "asc"
    value: object = None
    field: str = ""


def choose(request, columns, default=None):
    """(column, direction) the request asks for, or the page's default.

    `default` is a column name or None, where None keeps the order the rows
    arrive in and marks no heading.
    """
    sort = request.GET.get("sort", "")
    if sort not in columns:
        if default is None:
            return None, ""
        sort = default
    direction = request.GET.get("dir", "")
    if direction not in DIRECTIONS:
        direction = columns[sort].first
    return sort, direction


def ordered(rows, column, direction):
    """A list of rows by one column's `value`, blanks last."""
    value = column.value
    known = [row for row in rows if value(row) is not None]
    unknown = [row for row in rows if value(row) is None]
    return sorted(known, key=value, reverse=direction == "desc") + unknown


def order_queryset(queryset, column, direction):
    """A queryset by one column's `field`, blanks last, ties newest first."""
    expression = F(column.field)
    expression = (expression.desc(nulls_last=True) if direction == "desc"
                  else expression.asc(nulls_last=True))
    return queryset.order_by(expression, "-pk")


def headers(request, columns, sort, direction):
    """Each sortable heading: its link, and which way it sorts now, if it does."""
    kept = [(key, value) for key, value in request.GET.items()
            if key not in {"page", "sort", "dir"}]
    found = {}
    for name, column in columns.items():
        current = name == sort
        then = ("asc" if direction == "desc" else "desc") if current else column.first
        found[name] = {
            "label": column.label,
            "href": "?" + urlencode(kept + [("sort", name), ("dir", then)]),
            "direction": direction if current else "",
            "aria_sort": DIRECTIONS[direction] if current else "",
            "next": DIRECTIONS[then],
        }
    return found


def carried(sort, direction, columns, default=None):
    """The order as query parameters for page links, or none when it is the default."""
    if sort is None:
        return {}
    if sort == default and direction == columns[default].first:
        return {}
    return {"sort": sort, "dir": direction}


def key_order(key):
    """KAN-9 before KAN-10: the project, then its number as a number."""
    match = NUMBERED.match(key)
    return (match.group(1).casefold(), int(match.group(2))) if match else (key.casefold(), -1)


def priority_rank(priority):
    """Lower is more urgent; None for a priority this cannot place."""
    words = priority.casefold().replace("-", " ").split()
    if words and words[0].isdigit():
        return int(words[0])
    return next((PRIORITY_RANK[word] for word in words if word in PRIORITY_RANK), None)
