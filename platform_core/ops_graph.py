"""The operations graph: incidents, changes and what connects them, built by rules.

The knowledge graph holds documents and is publish-gated: a person decides when
a version answers questions. This is its other layer, and it is live. Incidents
and changes are imported hourly and triage cannot wait for someone to publish,
so the graph is rebuilt from them whenever they change - after an import, after
an incident is added by hand, and before anything reads it, if the records have
moved on since it was built.

It is built by rules only, never by a model: an incident is *on* a component
because its record says so, a change came *before* it because the timestamps
say so, two incidents are *similar* because `serviceops.score_pair` says so.
Every node points at the record it came from, and whatever reads an edge
verifies that record again - the same discipline as every citation here.

A person's verdict can add an edge (`confirmed`) that a rebuild keeps; nothing a
model says is ever written to it.
"""

import hashlib
import re
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import KnowledgeEntry, OperationsGraph, OpsEdge, OpsNode
from .serviceops import (
    CANDIDATE_LIMIT,
    CONCEPT_WORDS,
    incident_queryset,
    is_open,
    is_resolved,
    parse,
    same_event,
    score_pair,
    symptom_terms,
    term_weights,
    verified,
)

#: How many similar past incidents each incident keeps an edge to.
SIMILAR_PER_INCIDENT = 8
#: A change counts as "before" an incident if it started at most this long before.
BEFORE_WINDOW = timedelta(days=2)
#: Header fields that name a shared thing, the node kind, and the relation to it.
SHARED = (
    ("CI", "component", "on"),
    ("Service", "service", "in"),
    ("Assignment group", "group", "assigned"),
)


def change_queryset(app):
    """Imported change requests, which is what the connector's source says they are."""
    return KnowledgeEntry.objects.filter(
        application=app, active=True, source__icontains="change_request.do"
    )


def typed_queryset(app, kind):
    """Imported records whose header says what they are: "Type: <kind>"."""
    return KnowledgeEntry.objects.filter(application=app, active=True).filter(
        Q(content__contains=f"\nType: {kind}\n") | Q(content__contains=f"\nType: {kind}\r\n")
    )


def article_queryset(app):
    """Knowledge articles imported from ServiceNow's kb_knowledge table."""
    return typed_queryset(app, "Knowledge article")


def automation_queryset(app):
    """Automations from AWX, Rundeck, Azure Automation, ServiceNow flows or a list."""
    return typed_queryset(app, "Automation")


def article_live(fields, today):
    """ServiceNow's own review is the gate: Published, and not past its valid-to date.

    Deliberately not this platform's publish step. A KB article has already been
    reviewed and published where it is written; asking someone to publish it a
    second time here only delays the article a responder needs. Every workflow
    state is imported, so an article retired there leaves the graph at the next
    sync rather than lingering.
    """
    if fields.get("State", "").casefold() != "published":
        return False
    valid_to = _date(fields.get("Valid to"))
    return valid_to is None or valid_to.date() >= today


#: Automation states that mean it cannot be run as it stands.
UNUSABLE_STATES = {"inactive", "disabled", "new", "draft"}


def automation_usable(fields):
    return fields.get("State", "").casefold() not in UNUSABLE_STATES


def records_fingerprint(app):
    """Over every incident and change the graph is built from, id and digest,
    and which knowledge-graph version is published: publishing a new one
    re-links the documents."""
    from .graphs import published_revision

    rows = sorted(
        {
            (str(pk), digest)
            for queryset in (
                incident_queryset(app),
                change_queryset(app),
                article_queryset(app),
                automation_queryset(app),
            )
            for pk, digest in queryset.values_list("pk", "digest")
        }
    )
    published = published_revision(app.pk)
    rows.append(("published", str(published.pk) if published else ""))
    # An article's valid-to date passes without any record changing, so the day
    # is part of what the graph was built from.
    rows.append(("today", timezone.now().date().isoformat()))
    return hashlib.sha256(repr(rows).encode()).hexdigest()


def documents_only(entries):
    """Knowledge with a document behind it: uploads, links and folders.

    A connector import has none - it is a record, and records are the
    operations layer's own nodes, not passages about them.
    """
    return entries.filter(document__isnull=False)


#: Shorter names would join passages by accident: a CI called "api" is in every
#: document about any API.
MIN_NAME_LENGTH = 4
#: At most this many passages are joined to any one component or service.
PASSAGES_PER_NAME = 12


def passages(app, names):
    """Published knowledge-graph passages that name a component or service.

    `names` maps a case-folded name to its node key. Only the latest published
    revision, and only a quote whose source is active with an unchanged digest
    and which still contains the quote - the same checks `graph_citations`
    makes. A name matches whole, not inside a longer name: `carepath-api` is not
    in `carepath-api-green`.
    """
    import re

    from .graphs import published_revision, sources_for

    revision = published_revision(app.pk)
    wanted = {name: key for name, key in names.items() if len(name) >= MIN_NAME_LENGTH}
    if revision is None or not wanted:
        return []
    patterns = {
        name: re.compile(rf"(?<![\w-]){re.escape(name)}(?![\w-])", re.IGNORECASE) for name in wanted
    }
    edges = revision.data.get("edges", [])
    ids = {edge.get("knowledge_id") for edge in edges if edge.get("knowledge_id")}
    # Documents only. The published graph also holds imported tickets, whose own
    # "CI: carepath-api-green" header line would otherwise "mention" the
    # component - duplicating the operations layer and crowding out runbooks.
    live = {
        str(entry.pk): entry for entry in documents_only(sources_for(app.pk)).filter(pk__in=ids)
    }
    found, counts, seen = [], {}, set()
    for edge in edges:
        quote = edge.get("evidence") or ""
        entry = live.get(edge.get("knowledge_id"))
        if not quote or entry is None or entry.digest != edge.get("digest"):
            continue
        if quote not in entry.content:
            continue
        for name, pattern in patterns.items():
            if counts.get(name, 0) >= PASSAGES_PER_NAME or not pattern.search(quote):
                continue
            passage = f"passage:{entry.pk}:{hashlib.sha256(quote.encode()).hexdigest()[:12]}"
            if (passage, name) in seen:
                continue
            seen.add((passage, name))
            counts[name] = counts.get(name, 0) + 1
            line = edge.get("line")
            found.append(
                {
                    "key": passage,
                    "target": wanted[name],
                    "entry": entry,
                    "title": entry.title + (f" · line {line}" if line else ""),
                    "quote": quote[:900],
                    "graph_version": str(revision.number),
                }
            )
    return found


def ensure_current(app):
    """The graph's state row, rebuilt first if any record it was built from changed."""
    current = records_fingerprint(app)
    state = OperationsGraph.objects.filter(application=app).first()
    if state and state.fingerprint == current:
        return state
    return rebuild(app, current)


def _date(value):
    from .serviceops_triage import _date as parse_date

    return parse_date(value)


def _number(record):
    return record.fields.get("Number") or record.entry.title[:40]


def plan(app):
    """Every machine-built node and edge, from the records as they are now.

    Pure: reads, computes, writes nothing. Returns (nodes, edges) keyed so a
    rebuild can compare them with what is stored.
    """
    incidents = [
        parse(entry)
        for entry in incident_queryset(app).order_by("-created_at")[:CANDIDATE_LIMIT]
        if verified(entry)
    ]
    changes = [
        parse(entry)
        for entry in change_queryset(app).order_by("-created_at")[:CANDIDATE_LIMIT]
        if verified(entry)
    ]
    weights = term_weights(app)
    nodes = {}
    edges = {}

    def node(key, kind, label, entry=None, data=None):
        nodes.setdefault(
            key, {"kind": kind, "label": label[:300], "entry": entry, "data": data or {}}
        )
        return key

    def edge(source, target, relation, weight=0.0, detail=None):
        edges[(source, target, relation)] = {"weight": weight, "detail": detail or {}}

    def shared(record, key):
        for field, kind, relation in SHARED:
            value = " ".join(record.fields.get(field, "").split())
            if value:
                edge(key, node(f"{kind}:{value.casefold()}"[:300], kind, value), relation)

    keys = {}
    for record in incidents:
        key = node(
            f"incident:{record.entry.pk}",
            "incident",
            f"{_number(record)} · {record.entry.title}",
            entry=record.entry,
            data={
                "number": record.fields.get("Number", ""),
                "title": record.entry.title,
                "state": record.fields.get("State") or record.fields.get("Status", ""),
                "priority": record.fields.get("Priority", ""),
                "opened": record.fields.get("Opened", ""),
                "resolved": record.fields.get("Resolved", ""),
                "close_code": record.fields.get("Close code", ""),
                "open": not is_resolved(record.fields),
            },
        )
        keys[record.entry.pk] = key
        shared(record, key)
        for term in sorted(record.terms & CONCEPT_WORDS):
            edge(key, node(f"symptom:{term}", "symptom", term.replace("_", " ")), "symptom")

    for record in changes:
        key = node(
            f"change:{record.entry.pk}",
            "change",
            f"{_number(record)} · {record.entry.title}",
            entry=record.entry,
            data={
                "number": record.fields.get("Number", ""),
                "title": record.entry.title,
                "state": record.fields.get("State", ""),
                "start": record.fields.get("Start", ""),
                "end": record.fields.get("End", ""),
            },
        )
        shared(record, key)
        started = _date(record.fields.get("Start"))
        component = record.fields.get("CI", "").casefold()
        if not (started and component):
            continue
        for incident in incidents:
            opened = _date(incident.fields.get("Opened"))
            if not opened or incident.fields.get("CI", "").casefold() != component:
                continue
            gap = opened - started
            if timedelta(0) <= gap <= BEFORE_WINDOW:
                hours = round(gap.total_seconds() / 3600, 1)
                edge(key, keys[incident.entry.pk], "before", hours, {"hours_before": hours})

    resolved = [record for record in incidents if is_resolved(record.fields)]
    still_open = [record for record in incidents if is_open(record.fields)]
    for record in incidents:
        scored = []
        for other in resolved:
            if other.entry.pk == record.entry.pk:
                continue
            score, reasons, differences, alike = score_pair(record, other, weights)
            if score:
                scored.append((score, other, reasons, differences, alike))
        scored.sort(key=lambda row: (-row[0], -row[1].entry.created_at.timestamp()))
        for score, other, reasons, differences, alike in scored[:SIMILAR_PER_INCIDENT]:
            edge(
                keys[record.entry.pk],
                keys[other.entry.pk],
                "similar",
                score,
                {"reasons": reasons, "differences": differences, "similarity": round(alike, 2)},
            )
        for other in still_open:
            if other.entry.pk == record.entry.pk:
                continue
            match = same_event(record, other, weights)
            if match:
                edge(
                    keys[record.entry.pk],
                    keys[other.entry.pk],
                    "duplicate",
                    match["similarity"],
                    match,
                )

    link_articles_and_automations(app, incidents, keys, nodes, node, edge)

    # Documents: published passages that name a component or service by its
    # exact name. The passage points at the thing it names, so incident →
    # component → runbook is a path.
    names = {
        value["label"].casefold(): key
        for key, value in nodes.items()
        if value["kind"] in {"component", "service"}
    }
    for passage in passages(app, names):
        node(
            passage["key"],
            "passage",
            passage["title"],
            entry=passage["entry"],
            data={
                "title": passage["title"],
                "excerpt": passage["quote"],
                "graph_version": passage["graph_version"],
            },
        )
        edge(passage["key"], passage["target"], "mentions", 0.0, {"quote": passage["quote"]})
    return nodes, edges


#: Shorter automation names would be found inside unrelated close notes.
MIN_AUTOMATION_NAME = 6
KB_NUMBER = re.compile(r"\bKB\d{4,}\b", re.IGNORECASE)


def whole(name):
    return re.compile(rf"(?<![\w-]){re.escape(name)}(?![\w-])", re.IGNORECASE)


def link_articles_and_automations(app, incidents, keys, nodes, node, edge):
    """Knowledge articles and automations, joined by rules to what they are about.

    * An article *covers* the CI its record names, and any component or service
      whose exact name it uses.
    * An incident *cites* an article whose number it mentions - usually in its
      close notes, which is how a resolved incident teaches the next one.
    * An automation *targets* its CI and service, and *remediates* the symptoms
      it lists. An article that names it *references* it, and a resolved
      incident whose notes name it was *resolved by* it.

    Names match whole, never inside a longer word, and nothing a model said is
    used: the same discipline as every other edge here.
    """
    today = timezone.now().date()
    articles = [
        record
        for record in (
            parse(entry)
            for entry in article_queryset(app).order_by("-created_at")[:CANDIDATE_LIMIT]
            if verified(entry)
        )
        if article_live(record.fields, today)
    ]
    automations = [
        record
        for record in (
            parse(entry)
            for entry in automation_queryset(app).order_by("-created_at")[:CANDIDATE_LIMIT]
            if verified(entry)
        )
        if automation_usable(record.fields)
    ]
    if not articles and not automations:
        return

    def shared_key(kind, value):
        value = " ".join(value.split())
        return node(f"{kind}:{value.casefold()}"[:300], kind, value)

    names = {
        value["label"].casefold(): key
        for key, value in nodes.items()
        if value["kind"] in {"component", "service"} and len(value["label"]) >= MIN_NAME_LENGTH
    }
    name_patterns = {name: whole(name) for name in names}

    by_number = {}
    for record in articles:
        key = node(
            f"kb:{record.entry.pk}",
            "kb",
            f"{_number(record)} · {record.entry.title}",
            entry=record.entry,
            data={
                "number": record.fields.get("Number", ""),
                "title": record.entry.title,
                "state": record.fields.get("State", ""),
                "category": record.fields.get("Category", ""),
                "knowledge_base": record.fields.get("Knowledge base", ""),
                "valid_to": record.fields.get("Valid to", ""),
            },
        )
        if record.fields.get("Number"):
            by_number[record.fields["Number"].casefold()] = key
        if record.fields.get("CI"):
            edge(shared_key("component", record.fields["CI"]), key, "covers", 1.0, {"via": "CI"})
        text = f"{record.entry.title}\n{record.description}"
        for name, pattern in name_patterns.items():
            if pattern.search(text):
                edge(names[name], key, "covers", 0.5, {"via": "names it"})

    for record in incidents:
        for number in set(KB_NUMBER.findall(record.entry.content)):
            target = by_number.get(number.casefold())
            if target:
                edge(keys[record.entry.pk], target, "cites", 1.0, {"number": number.upper()})

    for record in automations:
        key = node(
            f"automation:{record.entry.pk}",
            "automation",
            record.entry.title,
            entry=record.entry,
            data={
                "number": record.fields.get("Number", ""),
                "title": record.entry.title,
                "platform": record.fields.get("Platform", ""),
                "automation_id": record.fields.get("Automation ID", ""),
                "risk": record.fields.get("Risk", ""),
                "approval": record.fields.get("Approval", ""),
                "inputs": record.fields.get("Inputs", ""),
                "state": record.fields.get("State", ""),
                "last_run": record.fields.get("Last run", ""),
                "last_status": record.fields.get("Last status", ""),
            },
        )
        for field, kind in (("CI", "component"), ("Service", "service")):
            if record.fields.get(field):
                edge(shared_key(kind, record.fields[field]), key, "targets", 1.0, {"via": field})
        listed = symptom_terms(record.fields.get("Symptoms", ""), record.description)
        for term in sorted(listed & CONCEPT_WORDS):
            symptom = node(f"symptom:{term}", "symptom", term.replace("_", " "))
            edge(symptom, key, "remediates", 1.0, {"symptom": term.replace("_", " ")})
        callers = [
            text
            for text in (record.entry.title, record.fields.get("Automation ID", ""))
            if len(text) >= MIN_AUTOMATION_NAME
        ]
        patterns = [whole(text) for text in callers]
        if not patterns:
            continue
        for article in articles:
            if any(pattern.search(article.description) for pattern in patterns):
                edge(f"kb:{article.entry.pk}", key, "references", 0.5)
        for incident in incidents:
            if not is_resolved(incident.fields):
                continue
            notes = f"{incident.fields.get('Close notes', '')}\n{incident.description}"
            if any(pattern.search(notes) for pattern in patterns):
                edge(keys[incident.entry.pk], key, "resolved_by", 1.0)


def rebuild(app, current=None):
    """Replace every machine-built node and edge, in one transaction.

    Readers see the old graph or the new one, never half. Edges a person made
    are kept, and so are the nodes they join.
    """
    current = current or records_fingerprint(app)
    nodes, edges = plan(app)
    with transaction.atomic():
        state, _ = OperationsGraph.objects.select_for_update().get_or_create(application=app)
        OpsEdge.objects.filter(application=app).exclude(relation__in=OpsEdge.PERSON_MADE).delete()
        kept = OpsEdge.objects.filter(application=app)
        joined = set(kept.values_list("source_id", flat=True)) | set(
            kept.values_list("target_id", flat=True)
        )
        OpsNode.objects.filter(application=app).exclude(key__in=nodes).exclude(
            pk__in=joined
        ).delete()
        existing = {item.key: item for item in OpsNode.objects.filter(application=app)}
        OpsNode.objects.bulk_create(
            [
                OpsNode(application=app, key=key, **value)
                for key, value in nodes.items()
                if key not in existing
            ]
        )
        for key, value in nodes.items():
            item = existing.get(key)
            entry_id = value["entry"].pk if value["entry"] else None
            if item and (item.label, item.data, item.entry_id) != (
                value["label"],
                value["data"],
                entry_id,
            ):
                OpsNode.objects.filter(pk=item.pk).update(
                    label=value["label"], data=value["data"], entry_id=entry_id
                )
        by_key = dict(OpsNode.objects.filter(application=app).values_list("key", "pk"))
        OpsEdge.objects.bulk_create(
            [
                OpsEdge(
                    application=app,
                    source_id=by_key[source],
                    target_id=by_key[target],
                    relation=relation,
                    weight=value["weight"],
                    detail=value["detail"],
                )
                for (source, target, relation), value in edges.items()
            ]
        )
        state.fingerprint = current
        state.built_at = timezone.now()
        state.node_count = OpsNode.objects.filter(application=app).count()
        state.edge_count = OpsEdge.objects.filter(application=app).count()
        state.save()
    return state


#: How much of each kind a walk returns: the pack stays within its 40-item bound.
WALK_LIMITS = {
    "precedents": 5,
    "changes": 5,
    "related": 5,
    "passages": 5,
    "confirmed": 3,
    "articles": 4,
    "automations": 4,
}


def _live(entry):
    """A record an edge reaches still holds: active, and its digest unchanged."""
    return entry is not None and verified(entry)


def _name(node):
    return node.data.get("number") or node.label.split(" · ")[0]


def path_text(path):
    return " → ".join(part for part in path if part)


def neighbourhood(app, incident):
    """Everything around one incident, found by walking its graph: the one reader.

    The incident page's Evidence section and triage's evidence pack both come
    from here, so they cannot disagree. Hop 1 is what the incident is on and in,
    the changes before it, similar resolved incidents, open incidents that may be
    the same event, and what people confirmed caused the similar ones. Hop 2 is
    document passages that mention its component or service.

    Every row carries the `path` that found it and is verified against its
    record as it is read: a record retired or edited since the build is skipped.
    """
    from .serviceops import fields_and_description, graph_rows, priority_tone

    ensure_current(app)
    result = {
        "center": None,
        "entities": [],
        "precedents": [],
        "changes": [],
        "related": [],
        "confirmed": [],
        "passages": [],
        "articles": [],
        "automations": [],
    }
    passages, result["graph_available"] = graph_rows(app, incident)
    center = OpsNode.objects.filter(application=app, key=f"incident:{incident.pk}").first()
    if center is None:
        # Past the build cap, or not an incident the graph holds: search only.
        result["passages"] = [dict(row, path=[]) for row in passages][: WALK_LIMITS["passages"]]
        return result
    result["center"] = center
    me = _name(center)
    outgoing = list(
        OpsEdge.objects.filter(application=app, source=center).select_related("target__entry")
    )
    incoming = OpsEdge.objects.filter(
        application=app, target=center, relation="before"
    ).select_related("source__entry")

    entities = {}
    similar_nodes = []
    for edge in outgoing:
        node = edge.target
        if edge.relation in {"on", "in", "assigned", "symptom"}:
            result["entities"].append({"node": node, "relation": edge.relation})
            entities[node.pk] = node
            continue
        if edge.relation not in {"similar", "duplicate"} or not _live(node.entry):
            continue
        entry = node.entry
        fields, description = fields_and_description(entry)
        if edge.relation == "similar":
            excerpt = fields.get("Close notes") or description[:450]
            if excerpt and excerpt not in entry.content:
                continue
            reasons = edge.detail.get("reasons", [])
            via = (
                fields.get("CI")
                if "same CI" in reasons
                else fields.get("Service")
                if "same service" in reasons
                else "similar symptoms"
            )
            similar_nodes.append(node)
            result["precedents"].append(
                {
                    "entry": entry,
                    "fields": fields,
                    "priority_tone": priority_tone(fields.get("Priority")),
                    "close_code": fields.get("Close code", ""),
                    "score": int(edge.weight),
                    "reasons": reasons,
                    "differences": edge.detail.get("differences", []),
                    "excerpt": excerpt,
                    "path": [me, via, _name(node)],
                }
            )
        else:
            shared = edge.detail.get("shared", [])
            result["related"].append(
                {
                    "entry": entry,
                    "fields": fields,
                    "same_fingerprint": edge.detail.get("same_fingerprint", False),
                    "similarity": edge.weight,
                    "shared": shared,
                    "path": [
                        me,
                        "same symptoms"
                        if edge.detail.get("same_fingerprint")
                        else ", ".join(shared[:3]),
                        _name(node),
                    ],
                }
            )
    for edge in incoming:
        node = edge.source
        if not _live(node.entry):
            continue
        fields, description = fields_and_description(node.entry)
        result["changes"].append(
            {
                "entry": node.entry,
                "fields": fields,
                "excerpt": description[:500] or node.entry.title,
                "hours_before": edge.weight,
                "path": [me, fields.get("CI", ""), f"{_name(node)} ({edge.weight:g} h before)"],
            }
        )
    result["precedents"].sort(
        key=lambda row: (-row["score"], -row["entry"].created_at.timestamp(), str(row["entry"].pk))
    )
    result["related"].sort(key=lambda row: (not row["same_fingerprint"], -row["similarity"]))
    result["changes"].sort(key=lambda row: (row["hours_before"], str(row["entry"].pk)))
    result["confirmed"] = confirmed_rows(app, center, similar_nodes)
    result["passages"] = passage_rows(app, entities, passages, me)
    result["articles"] = article_rows(app, center, entities, similar_nodes, me)
    result["automations"] = automation_rows(
        app, center, entities, similar_nodes, result["articles"], me
    )
    for kind, limit in WALK_LIMITS.items():
        result[kind] = result[kind][:limit]
    return result


def confirmed_rows(app, center, similar_nodes):
    """What people confirmed caused this incident, or the incidents most like it.

    The only evidence here a person asserted rather than a rule derived. Shown
    as "confirmed by", never as fact, and verified like everything else.
    """
    from .serviceops import fields_and_description

    ranked = [center] + similar_nodes
    order = {node.pk: index for index, node in enumerate(ranked)}
    rows = []
    for edge in (
        OpsEdge.objects.filter(application=app, relation="confirmed", source__in=ranked)
        .select_related("source", "target__entry", "created_by")
        .order_by("-created_at")
    ):
        node = edge.target
        if not _live(node.entry):
            continue
        fields, description = fields_and_description(node.entry)
        excerpt = node.data.get("excerpt") or description[:500] or node.entry.title
        if excerpt not in node.entry.content:
            continue
        mine = edge.source_id == center.pk
        rows.append(
            {
                "entry": node.entry,
                "fields": fields,
                "excerpt": excerpt,
                "cause_of": _name(edge.source),
                "own": mine,
                "confirmed_by": edge.created_by.username if edge.created_by else "a former user",
                "confirmed_at": edge.created_at,
                "note": edge.detail.get("note", ""),
                "rank": order[edge.source_id],
                "path": [_name(center)]
                + ([] if mine else [f"similar {_name(edge.source)}"])
                + [f"confirmed cause: {_name(node)}"],
            }
        )
    rows.sort(key=lambda row: (row["rank"], -row["confirmed_at"].timestamp()))
    seen, unique = set(), []
    for row in rows:
        if row["entry"].pk not in seen:
            seen.add(row["entry"].pk)
            unique.append(row)
    return unique


def article_rows(app, center, entities, similar_nodes, me):
    """Knowledge articles around an incident, the strongest reason first.

    Cited by this incident; then cited by a similar resolved one (how it was
    fixed last time); then covering the component or service it is on.
    """
    from .serviceops import fields_and_description

    found = {}

    def add(node, rank, why, path):
        if node.kind != "kb" or not _live(node.entry):
            return
        current = found.get(node.pk)
        if current is None or rank < current["rank"]:
            fields, description = fields_and_description(node.entry)
            found[node.pk] = {
                "entry": node.entry,
                "fields": fields,
                "excerpt": description[:900] or node.entry.title,
                "reasons": [why],
                "path": path,
                "rank": rank,
            }
        elif why not in current["reasons"]:
            current["reasons"].append(why)

    for edge in OpsEdge.objects.filter(application=app, relation="cites", source=center):
        add(edge.target, 0, "cited by this incident", [me, _name(edge.target)])
    for edge in OpsEdge.objects.filter(
        application=app, relation="cites", source__in=similar_nodes
    ).select_related("source", "target__entry"):
        add(
            edge.target,
            1,
            f"cited when {_name(edge.source)} was resolved",
            [me, f"similar {_name(edge.source)}", _name(edge.target)],
        )
    for edge in OpsEdge.objects.filter(
        application=app, relation="covers", source__in=list(entities)
    ).select_related("source", "target__entry"):
        add(
            edge.target,
            2,
            f"covers {edge.source.label}",
            [me, edge.source.label, _name(edge.target)],
        )
    rows = sorted(found.values(), key=lambda row: (row["rank"], row["entry"].title))
    return [row for row in rows if row["excerpt"] in row["entry"].content]


def automation_rows(app, center, entities, similar_nodes, articles, me):
    """Automations that may help, the strongest reason first.

    One that resolved a similar incident before outranks one that merely targets
    the same component; each reason it has is listed, so a responder sees why.
    Suggestions only: running one is a person's decision, and here only ever
    simulated.
    """
    from .serviceops import fields_and_description

    found = {}

    def add(node, points, why, path):
        if node.kind != "automation" or not _live(node.entry):
            return
        row = found.get(node.pk)
        if row is None:
            fields, description = fields_and_description(node.entry)
            row = found[node.pk] = {
                "entry": node.entry,
                "node": node,
                "fields": fields,
                "excerpt": description[:600] or node.entry.title,
                "last_status": fields.get("Last status", ""),
                "reasons": [],
                "path": path,
                "points": 0,
            }
        if why not in row["reasons"]:
            row["reasons"].append(why)
            row["points"] += points

    for edge in OpsEdge.objects.filter(
        application=app, relation="resolved_by", source__in=similar_nodes
    ).select_related("source", "target__entry"):
        add(
            edge.target,
            3,
            f"resolved {_name(edge.source)} before",
            [me, f"similar {_name(edge.source)}", edge.target.label],
        )
    for edge in OpsEdge.objects.filter(
        application=app, relation__in=("targets", "remediates"), source__in=list(entities)
    ).select_related("source", "target__entry"):
        why = (
            f"remediates {edge.source.label}"
            if edge.relation == "remediates"
            else f"targets {edge.source.label}"
        )
        add(edge.target, 2 if edge.relation == "targets" else 1, why,
            [me, edge.source.label, edge.target.label])
    article_nodes = OpsNode.objects.filter(
        application=app, kind="kb", entry__in=[row["entry"] for row in articles]
    )
    for edge in OpsEdge.objects.filter(
        application=app, relation="references", source__in=article_nodes
    ).select_related("source", "target__entry"):
        add(
            edge.target,
            1,
            f"named in {_name(edge.source)}",
            [me, _name(edge.source), edge.target.label],
        )
    rows = sorted(found.values(), key=lambda row: (-row["points"], row["entry"].title))
    return [row for row in rows if row["excerpt"] in row["entry"].content]


def passage_rows(app, entities, searched, me):
    """Document passages: those that name this incident's component or service
    first, reached through the graph, then the knowledge graph's own text search."""
    from .graph_ai import published_version

    version = published_version(app.pk)
    rows = []
    seen = set()
    if version is not None and entities:
        for edge in OpsEdge.objects.filter(
            application=app, relation="mentions", target__in=list(entities)
        ).select_related("source__entry", "target"):
            node = edge.source
            data = node.data
            if data.get("graph_version") != version or not _live(node.entry):
                continue
            if data.get("excerpt", "") not in node.entry.content:
                continue
            key = (str(node.entry.pk), data["excerpt"])
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "id": str(node.entry.pk),
                    "title": data.get("title") or node.entry.title,
                    "excerpt": data["excerpt"],
                    "graph_version": version,
                    "path": [me, edge.target.label, data.get("title") or node.entry.title],
                }
            )
    documented = {
        str(pk)
        for pk in documents_only(KnowledgeEntry.objects.filter(application=app))
        .filter(pk__in=[row["id"] for row in searched])
        .values_list("pk", flat=True)
    }
    for row in searched:
        key = (row["id"], row["excerpt"])
        if row["id"] in documented and key not in seen:
            seen.add(key)
            rows.append(dict(row, path=[me, "text search", row["title"]]))
    return rows


#: The incident page's map, in SVG units.
MAP_WIDTH, MAP_HEIGHT = 760, 420


def neighbourhood_map(walk):
    """Positions for the incident page's map, laid out here so it needs no script.

    The incident in the middle; what it is on, in and shows around it; the
    evidence on an outer ring, each joined to the thing it was reached through.
    """
    import math

    center = walk["center"]
    if center is None:
        return None
    cx, cy = MAP_WIDTH / 2, MAP_HEIGHT / 2
    items = [
        {
            "id": "c",
            "kind": "incident",
            "label": _name(center),
            "title": center.label,
            "x": cx,
            "y": cy,
            "center": True,
        }
    ]
    lines = []
    reached = {}
    inner = {}
    ring = [item for item in walk["entities"] if item["relation"] != "assigned"]
    for index, item in enumerate(ring):
        angle = -math.pi / 2 + index * 2 * math.pi / max(1, len(ring))
        node = item["node"]
        key = f"e{index}"
        reached[node.label.casefold()] = key
        inner[key] = angle
        items.append(
            {
                "id": key,
                "kind": node.kind,
                "label": node.label[:28],
                "title": node.label,
                "x": cx + math.cos(angle) * 140,
                "y": cy + math.sin(angle) * 95,
            }
        )
        lines.append({"a": "c", "b": key, "label": dict(OpsEdge.RELATIONS)[item["relation"]]})
    evidence = (
        [("confirmed", row, "confirmed cause") for row in walk["confirmed"]]
        + [("change", row, f"{row['hours_before']:g} h before") for row in walk["changes"]]
        + [("precedent", row, "similar, resolved") for row in walk["precedents"]]
        + [("related", row, "maybe the same event") for row in walk["related"]]
        + [("passage", row, "mentions it") for row in walk["passages"] if row["path"]]
    )
    # Each piece of evidence sits on the outer ring beside what it was reached
    # through, so its line runs outward instead of across the middle. Evidence
    # reached from the incident itself goes in the widest gap between the
    # inner nodes. A sweep then keeps neighbours at least one step apart.
    ordered = sorted(inner.values())
    if ordered:
        gaps = [
            ((ordered[(i + 1) % len(ordered)] - here) % (2 * math.pi), here)
            for i, here in enumerate(ordered)
        ]
        width, start = max(gaps)
        free = start + (width or 2 * math.pi) / 2
    else:
        free = -math.pi / 2
    step = 2 * math.pi / max(1, len(evidence))
    parents = []
    for _, row, _ in evidence:
        via = row["path"][1].casefold() if len(row["path"]) > 2 else ""
        parents.append(reached.get(via, "c"))
    wanted = [inner.get(parent, free) for parent in parents]
    order = sorted(range(len(evidence)), key=lambda i: (wanted[i], i))
    placed = {}
    previous = None
    for i in order:
        angle = wanted[i] if previous is None else max(wanted[i], previous + step)
        placed[i] = previous = angle
    if placed:
        drift = sum(placed[i] - wanted[i] for i in placed) / len(placed)
        placed = {i: angle - drift for i, angle in placed.items()}
    for index, (kind, row, label) in enumerate(evidence):
        angle = placed[index]
        key = f"v{index}"
        entry_id = row["entry"].pk if "entry" in row else row["id"]
        number = row["fields"].get("Number") if "fields" in row else ""
        title = row["entry"].title if "entry" in row else row["title"]
        items.append(
            {
                "id": key,
                "kind": kind,
                "label": (number or title)[:24],
                "title": title,
                "entry": entry_id,
                "incident": kind == "related",
                "x": cx + math.cos(angle) * 320,
                "y": cy + math.sin(angle) * 175,
            }
        )
        lines.append({"a": parents[index], "b": key, "label": label})
    where = {item["id"]: item for item in items}
    for line in lines:
        start, end = where[line["a"]], where[line["b"]]
        line.update(
            x1=round(start["x"]),
            y1=round(start["y"]),
            x2=round(end["x"]),
            y2=round(end["y"]),
            kind=end["kind"],
        )
    for item in items:
        item["x"], item["y"] = round(item["x"]), round(item["y"])
        # Text sits below a node on the lower half, above it on the upper half.
        item["ty"] = item["y"] + (24 if item["y"] >= cy else -16)
    return {"width": MAP_WIDTH, "height": MAP_HEIGHT, "items": items, "lines": lines}


def _because(edge):
    """An edge's reason in words, for the explorer's inspector."""
    detail = edge.detail or {}
    if edge.relation == "similar":
        return " · ".join(detail.get("reasons", []) + detail.get("differences", []))
    if edge.relation == "duplicate":
        return (
            "Same symptoms"
            if detail.get("same_fingerprint")
            else "Shared symptoms: " + ", ".join(detail.get("shared", []))
        )
    if edge.relation == "before":
        return f"Started {edge.weight:g} hours before the incident opened"
    if edge.relation == "mentions":
        return detail.get("quote", "")
    if edge.relation == "confirmed":
        who = edge.created_by.username if edge.created_by else "a former user"
        return f"Confirmed by {who}" + (f": {detail['note']}" if detail.get("note") else "")
    return ""


def explorer_data(app):
    """The whole operations graph in the knowledge explorer's shape.

    Current first, and a node whose record no longer verifies is left out with
    every edge that touches it: the explorer shows what triage would use.
    """
    ensure_current(app)
    labels = dict(OpsEdge.RELATIONS)
    nodes = {}
    for node in OpsNode.objects.filter(application=app).select_related("entry"):
        if node.entry_id and not _live(node.entry):
            continue
        item = {"id": str(node.pk), "label": node.label, "kind": node.kind}
        if node.entry_id:
            item["knowledge_id"] = str(node.entry_id)
        nodes[node.pk] = item
    edges = [
        {
            "source": str(edge.source_id),
            "target": str(edge.target_id),
            "relation": labels[edge.relation],
            "evidence": _because(edge),
            "confirmed": edge.relation in OpsEdge.PERSON_MADE,
        }
        for edge in OpsEdge.objects.filter(application=app).select_related("created_by")
        if edge.source_id in nodes and edge.target_id in nodes
    ]
    return {"layer": "operations", "nodes": list(nodes.values()), "edges": edges}


def confirm_cause(app, incident, cause, verdict, quote):
    """A person's verdict, written into the graph: `incident` was caused by `cause`.

    The one edge a person makes. A rebuild keeps it, and it goes when its
    verdict is retracted. The cause is joined as the node the graph already has
    for that record; a document passage the graph does not hold yet gets a node
    of its own, carrying the quote the idea cited, which is re-checked on read.
    """
    ensure_current(app)
    source = OpsNode.objects.filter(application=app, key=f"incident:{incident.pk}").first()
    if source is None:
        return None
    target = OpsNode.objects.filter(application=app, entry=cause).exclude(kind="passage").first()
    if target is None:
        target, _ = OpsNode.objects.update_or_create(
            application=app,
            key=f"cause:{cause.pk}",
            defaults={
                "kind": "passage",
                "label": cause.title[:300],
                "entry": cause,
                "data": {"title": cause.title, "excerpt": quote},
            },
        )
    edge, _ = OpsEdge.objects.update_or_create(
        source=source,
        target=target,
        relation="confirmed",
        defaults={
            "application": app,
            "verdict": verdict,
            "created_by": verdict.user,
            "weight": 1.0,
            "detail": {"note": verdict.note, "verdict": verdict.verdict},
        },
    )
    return edge


def clear(application_ids):
    """Remove the operations graph of these applications entirely, confirmed edges too."""
    OpsEdge.objects.filter(application_id__in=application_ids).delete()
    OpsNode.objects.filter(application_id__in=application_ids).delete()
    OperationsGraph.objects.filter(application_id__in=application_ids).delete()
