"""Imported tickets, listed as tickets: Knowledge → Tickets.

A ticket is the same `KnowledgeEntry` the graph cites, Code Factory plans from
and ServiceOps triages - this is a way of reading those rows, not a store of
its own. It exists because the Sources table was built for files: a Jira issue
there said "Imported" for its file type and "Imported text" for its content,
and its key, status and priority were a click away. One import of a hundred
issues also pushed every uploaded document off the first pages.

What counts as a ticket is decided by the connector that imported it, with the
namespace test `connectors.prune` and `source_library.connector_index` use: a
connector of a work-item kind claims every record under its prefix. ServiceNow
knowledge articles and automation flows read like documents and runbooks, so
they stay on Documents; so does a record whose connector was removed, which no
connector claims.
"""

import re
from collections import Counter
from datetime import UTC
from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.db.models import Q
from django.urls import reverse
from django.utils.dateparse import parse_datetime

from .connector_kinds import KINDS, read_header
from .models import Connector, KnowledgeEntry
from .serviceops import incident_url
from .services import feature_enabled
from .source_library import SCORED_KINDS, graph_usage, imported_label, personal
from .ticket_quality import assess

#: Connector kinds whose every record is a work item.
TICKET_KINDS = {"jira", "github"}
#: ServiceNow tables whose records are not work items: knowledge articles and
#: Flow Designer flows. Every other table - incident, problem, change_request,
#: a catalogue task - is.
NOT_TICKET_TABLES = {"kb_knowledge", "sys_hub_flow"}
PAGE_SIZE = 50
#: Priority words in order of urgency, Jira's and ServiceNow's together.
#: ServiceNow also numbers them ("2 - High"), and the number wins.
PRIORITY_RANK = {"blocker": 0, "highest": 1, "critical": 1, "high": 2, "medium": 3,
                 "moderate": 3, "low": 4, "lowest": 5, "planning": 5, "trivial": 5}
NUMBERED = re.compile(r"^(.*?)(\d+)$")


def ticket_connector(connector):
    """Whether the records this connector imports are tickets."""
    if connector.kind in TICKET_KINDS:
        return True
    if connector.kind == "servicenow":
        return (connector.config or {}).get("table", "") not in NOT_TICKET_TABLES
    return False


def ticket_connectors(app):
    """(connector, record prefix) for each connector whose records are tickets.

    Kept on the application object, which lives for one request: the list, the
    tab and the page's note each ask, and each asking cost a query.
    """
    from .code_factory import connector_prefix

    if "_ticket_connectors" in app.__dict__:
        return app.__dict__["_ticket_connectors"]
    found = []
    for connector in Connector.objects.filter(application=app).order_by("name"):
        if connector.kind in KINDS and ticket_connector(connector):
            prefix = connector_prefix(connector)
            if prefix:
                found.append((connector, prefix))
    app.__dict__["_ticket_connectors"] = found
    return found


def ticket_filter(app, connectors=None):
    """The condition that picks out an application's tickets, or None when it has none."""
    prefixes = {prefix for _, prefix in (ticket_connectors(app) if connectors is None
                                         else connectors)}
    if not prefixes:
        return None
    condition = Q()
    for prefix in sorted(prefixes):
        condition |= Q(source__startswith=prefix)
    return condition


def ticket_entries(app, connectors=None):
    """Active ticket entries, standalone records only, or an empty queryset."""
    condition = ticket_filter(app, connectors)
    entries = KnowledgeEntry.objects.filter(
        application=app, active=True, document__isnull=True
    )
    return entries.filter(condition) if condition is not None else entries.none()


def tab_count(app):
    """The Tickets tab's count, or None when the application has no tab to show."""
    if "_ticket_count" not in app.__dict__:
        connectors = ticket_connectors(app)
        app.__dict__["_ticket_count"] = (
            ticket_entries(app, connectors).count() if connectors else None
        )
    return app.__dict__["_ticket_count"]


def short_label(kind, connector):
    """"ServiceNow · Incidents": the system, then what the connector adds to it.

    A connector named after its system ("ServiceNow incidents") would otherwise
    say the system twice on a badge with little room for once.
    """
    name = (connector.name if connector else "").strip()
    if name.casefold().startswith(kind.label.casefold()):
        name = name[len(kind.label):].strip(" -·:")
        name = name[:1].upper() + name[1:]
    return f"{kind.label} · {name}" if name else kind.label


def when(value):
    """A header date as a datetime, or None: Jira writes 2026-10-03T22:28:21.435+0530,
    ServiceNow 2026-10-01 12:00:00, GitHub 2026-10-01T12:00:00Z."""
    parsed = parse_datetime(value.strip()) if value else None
    # ServiceNow's carries no offset; the API answers in UTC. Naive and aware
    # cannot be compared, and the column sorts on this.
    if parsed and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


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


#: Sortable columns: label, how a row orders, and the first click's direction.
#: Priority sorts by urgency, readiness by score, the rest by their text.
COLUMNS = {
    "key": ("Key", lambda row: key_order(row["key"]) if row["key"] else None, "asc"),
    "title": ("Title", lambda row: row["entry"].title.casefold(), "asc"),
    "type": ("Type", lambda row: row["type"].casefold() or None, "asc"),
    "status": ("Status", lambda row: row["status"].casefold() or None, "asc"),
    # Most urgent first is "descending" priority, so rank is negated.
    "priority": ("Priority", lambda row: (-rank if (rank := priority_rank(row["priority"]))
                                          is not None else None), "desc"),
    "readiness": ("Readiness", lambda row: row["quality"]["score"] if row["quality"] else None,
                  "asc"),
    "updated": ("Updated", lambda row: row["updated"], "desc"),
}
DEFAULT_SORT = "updated"


def ordered(rows, column, direction):
    """Rows by one column; a ticket with nothing to sort on goes last either way."""
    value = COLUMNS[column][1]
    known = [row for row in rows if value(row) is not None]
    unknown = [row for row in rows if value(row) is None]
    return sorted(known, key=value, reverse=direction == "desc") + unknown


def headers(chosen, sort, direction, readiness):
    """The header row: each sortable column's link, and which way it sorts now.

    A link keeps the filters and starts at page one; clicking the column that
    sorts the table turns it round.
    """
    found = []
    for column, (label, _, first) in COLUMNS.items():
        if column == "readiness" and not readiness:
            continue
        current = column == sort
        then = ("asc" if direction == "desc" else "desc") if current else first
        query = {key: chosen[key] for key in ("connector", "status", "type", "q") if chosen[key]}
        query.update(sort=column, dir=then)
        found.append({
            "column": column,
            "label": label,
            "href": "?" + urlencode(query),
            "direction": direction if current else "",
            "aria_sort": ({"asc": "ascending", "desc": "descending"}[direction]
                          if current else ""),
            "next": "ascending" if then == "asc" else "descending",
        })
    return found


def describe(entry, fields, connectors):
    """What the row shows of a ticket, from its own header."""
    matched = [connector for connector, prefix in connectors if entry.source.startswith(prefix)]
    kind = KINDS[matched[0].kind] if matched else None
    return {
        "entry": entry,
        "key": fields.get("Key") or fields.get("Number") or "",
        "type": fields.get("Type") or ("Issue" if kind and kind.key == "github" else ""),
        "status": fields.get("Status") or fields.get("State") or "",
        "priority": fields.get("Priority") or "",
        "updated": when(fields.get("Updated") or fields.get("Opened")) or entry.created_at,
        "kind": kind,
        "connectors": matched,
        # The same words the record page uses for where it came from, and
        # the badge's shorter form of them.
        "origin": imported_label(kind, matched[0] if len(matched) == 1 else None),
        "source": short_label(kind, matched[0] if len(matched) == 1 else None) if kind else "",
        # The same test the record page applies before linking out.
        "link": entry.source if entry.source.startswith(("https://", "http://")) else "",
    }


def library(app, grant, request):
    """Everything the Tickets page shows: the filtered page of tickets and its toolbar."""
    connectors = ticket_connectors(app)
    entries = list(
        ticket_entries(app, connectors).order_by("-created_at", "-pk").only(
            "pk", "title", "content", "source", "created_at", "digest", "sensitivity",
            "personal_data", "masked", "application_id",
        )
    )
    rows = [describe(entry, read_header(entry.content), connectors) for entry in entries]
    for row in rows:
        entry = row["entry"]
        scored = row["kind"] is not None and row["kind"].key in SCORED_KINDS
        row["quality"] = assess(entry.title, entry.content) if scored else None
    chips = [
        {"connector": connector, "label": short_label(KINDS[connector.kind], connector),
         "count": sum(connector in row["connectors"] for row in rows)}
        for connector, _ in connectors
    ]
    statuses = sorted({row["status"] for row in rows if row["status"]}, key=str.casefold)
    types = sorted({row["type"] for row in rows if row["type"]}, key=str.casefold)
    chosen = {
        "connector": request.GET.get("connector", ""),
        "status": request.GET.get("status", ""),
        "type": request.GET.get("type", ""),
        "q": request.GET.get("q", "").strip()[:200],
    }
    if chosen["connector"]:
        rows = [row for row in rows
                if any(str(c.pk) == chosen["connector"] for c in row["connectors"])]
    if chosen["status"]:
        rows = [row for row in rows if row["status"] == chosen["status"]]
    if chosen["type"]:
        rows = [row for row in rows if row["type"] == chosen["type"]]
    if chosen["q"]:
        needle = chosen["q"].casefold()
        rows = [row for row in rows
                if needle in row["entry"].title.casefold()
                or needle in row["entry"].content.casefold()
                or needle in row["key"].casefold()]

    sort = request.GET.get("sort", "")
    if sort not in COLUMNS:
        sort = DEFAULT_SORT
    direction = request.GET.get("dir", "")
    if direction not in {"asc", "desc"}:
        direction = COLUMNS[sort][2]
    rows = ordered(rows, sort, direction)

    verdicts = Counter(row["quality"]["verdict"] for row in rows if row["quality"])
    page = Paginator(rows, PAGE_SIZE).get_page(request.GET.get("page"))
    shown = list(page.object_list)
    usage = graph_usage(app, [row["entry"] for row in shown])
    can_plan = feature_enabled("code_factory", app) and grant.role in {"owner", "contributor"}
    can_triage = feature_enabled("service_ops", app)
    for row in shown:
        entry = row["entry"]
        row["personal"] = personal(entry)
        row["versions"] = usage.get(str(entry.pk), [])
        # A restricted ticket is read by no model, so neither offers to start one.
        usable = entry.sensitivity != "restricted"
        row["plan_url"] = (
            f"{reverse('plans', args=[app.pk])}?ticket={entry.pk}#run-ticket"
            if can_plan and usable else ""
        )
        row["triage_url"] = (
            incident_url(app, entry.pk)
            if can_triage and usable and row["type"] == "Incident" else ""
        )
    page.object_list = shown
    return {
        "page": page,
        "chips": chips,
        "statuses": statuses,
        "types": types,
        "chosen": chosen,
        "query": chosen["q"],
        # The filters other than the search, and the order, carried across pages.
        "extra_query": urlencode(
            {key: chosen[key] for key in ("connector", "status", "type") if chosen[key]}
            | ({"sort": sort, "dir": direction}
               if (sort, direction) != (DEFAULT_SORT, COLUMNS[DEFAULT_SORT][2]) else {})
        ),
        "sort": sort,
        "direction": direction,
        "filtered": any(chosen.values()),
        "total": len(entries),
        "matching": page.paginator.count,
        # Jira tickets only are scored (`SCORED_KINDS`), so a board of GitHub
        # issues gets no column of dashes.
        "readiness_column": any(row["quality"] for row in shown),
        "columns": {column["column"]: column for column in headers(
            chosen, sort, direction, any(row["quality"] for row in shown))},
        # Over every ticket the filters leave, not only this page.
        "readiness": {
            "ready": verdicts["Ready"],
            "needs_work": verdicts["Needs work"],
            "not_ready": verdicts["Not ready"],
        } if verdicts else None,
        "has_connectors": bool(connectors),
    }
