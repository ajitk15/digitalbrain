"""A graph version's sources, nodes and edges as rows, each held once for every version it is in.

Phase 3 of the scale work. A `GraphRevision` keeps its whole graph in one JSON
value, and so does every version after it: a library of a few thousand sources
writes the same nodes again on every rebuild, and anything that wants one part
of a version - which sources it read, the edges of one source, the edges a
question names - reads all of it. `GraphElement` holds each element once, with
the range of version numbers it belongs to: a rebuild that changes one source
closes the rows that source no longer has and adds the ones it now has, and
every other row stays open. Version N is the rows with `first_number <= N` and
`last_number` empty or `>= N`, in `key` order.

**Order is kept, so a version reads back exactly** - the same lists, in the same
order, as its JSON. Ranking breaks ties by position and one-hop expansion walks
edges in list order, so a reader that lost order could not promise the answers
the JSON gives. Each occurrence has a `key` from a sparse integer space; a row
stays open only while it keeps its place relative to the other rows that stay,
and new rows take keys in the gaps between them. When a gap is used up the
version is written afresh, which costs one full write and never a wrong order.

The JSON is still written. Edges also carry what ranking needs - their text,
its terms, the references they name, whether the walk may follow them - so
`RowsGraph` can answer from the database what `graph_ai.prepare` answers in
memory (`graph_candidates = "rows"`; see `graph_ai.citations_from`).

Versions are recorded in number order, which is how `graphs.rebuild` numbers
them. One older than the newest recorded is left unrecorded rather than written
into ranges that already moved past it, and readers fall back to its JSON. The
maintenance lane records the rest (`record_step`).
"""

import hashlib
import json
import logging
from bisect import bisect_left
from collections import Counter, defaultdict, deque

from django.db import connection, transaction
from django.db.models import Count, Max, Q, Sum

from .models import Application, GraphElement, GraphRevision

logger = logging.getLogger(__name__)

#: The parts of a version's data that are rows: the key in `GraphRevision.data`
#: is the element's name.
ELEMENTS = ("sources", "nodes", "edges")
BATCH = 1000
#: Keys are spread across this space, leaving room to insert between them.
KEY_SPACE = 2**62


def digest_of(*parts):
    return hashlib.sha256(
        json.dumps(parts, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def padded(values):
    """Space-separated with a space at each end, so `LIKE '% term %'` is a whole match."""
    return f" {' '.join(values)} " if values else ""


def derive(data):
    """{element: [(body, digest, columns)]} in list order, as `graph_ai.prepare` reads it.

    An edge's text, terms and references come from the nodes and sources of the
    same version, so they are part of its digest: a node relabelled under an
    unchanged edge makes it a new row rather than one with stale terms.
    """
    from .ranking import identifiers, terms

    data = data or {}
    nodes = {n["id"]: n for n in data.get("nodes") or [] if isinstance(n, dict) and "id" in n}
    titles = {
        s["id"]: s.get("title") or ""
        for s in data.get("sources") or []
        if isinstance(s, dict) and "id" in s
    }
    derived = {}
    for element in ELEMENTS:
        items = []
        for body in data.get(element) or []:
            if not isinstance(body, dict):
                continue
            source = body.get("id") if element == "sources" else body.get("knowledge_id")
            columns = {"knowledge_id": str(source)[:64] if source else ""}
            if element == "edges":
                start, end = nodes.get(body.get("source")), nodes.get(body.get("target"))
                columns.update(start_node=str(body.get("source", ""))[:64],
                               end_node=str(body.get("target", ""))[:64])
                if start and end:
                    text = f"{start['label']} {body['relation']} {end['label']}"
                    split = terms(text)
                    names = frozenset(
                        identifiers(f"{text} {body.get('evidence', '')}")
                    ) - frozenset(identifiers(titles.get(body.get("knowledge_id"), "")))
                    columns.update(
                        text=text,
                        terms=padded(split),
                        length=len(split),
                        names=padded(sorted(names)),
                        typed=bool(body.get("inferred"))
                        and start.get("kind") == "entity"
                        and end.get("kind") == "entity",
                    )
            digest = digest_of(body, {k: v for k, v in columns.items() if k != "knowledge_id"})
            items.append((body, digest, columns))
        derived[element] = items
    return derived


def increasing(keys):
    """Indices of a longest strictly increasing run of the keys that are not None."""
    tails, tail_index, previous = [], [], {}
    for index, key in enumerate(keys):
        if key is None:
            continue
        place = bisect_left(tails, key)
        if place == len(tails):
            tails.append(key)
            tail_index.append(index)
        else:
            tails[place] = key
            tail_index[place] = index
        previous[index] = tail_index[place - 1] if place else None
    found, index = [], tail_index[-1] if tail_index else None
    while index is not None:
        found.append(index)
        index = previous[index]
    return set(found)


def spaced(count, low=0, high=None):
    """`count` keys strictly between low and high, evenly apart, or None if they do not fit."""
    high = KEY_SPACE if high is None else high
    if high - low - 1 < count:
        return None
    step = (high - low) // (count + 1)
    return [low + step * (n + 1) for n in range(count)]


def placed(items, opened):
    """(keys, which items keep their row, kept row ids), or None to write the list afresh."""
    pool = defaultdict(deque)
    for pk, digest, key in opened:
        pool[digest].append((pk, key))
    paired = [pool[digest].popleft() if pool[digest] else None for _, digest, _ in items]
    kept = increasing([pair[1] if pair else None for pair in paired])
    keys = [paired[index][1] if index in kept else None for index in range(len(items))]
    index = 0
    while index < len(keys):
        if keys[index] is not None:
            index += 1
            continue
        end = index
        while end < len(keys) and keys[end] is None:
            end += 1
        low = keys[index - 1] if index else 0
        high = keys[end] if end < len(keys) else KEY_SPACE
        gap = spaced(end - index, low, high)
        if gap is None:
            return None
        keys[index:end] = gap
        index = end
    return keys, [index in kept for index in range(len(items))], {paired[i][0] for i in kept}


def newest_recorded(app_id):
    return GraphRevision.objects.filter(application_id=app_id, stored=True).aggregate(
        latest=Max("number")
    )["latest"]


def record(revision):
    """Write a just-saved version's elements as rows. True when it was recorded.

    Call it inside the transaction that saved the version, so a version and its
    rows commit together or not at all.
    """
    app_id, number = revision.application_id, revision.number
    # One writer per application: a rebuild saving a version and the backfill
    # recording an older one would otherwise both close the same open rows.
    list(Application.objects.select_for_update().filter(pk=app_id).values_list("pk"))
    latest = newest_recorded(app_id)
    if latest is not None and latest >= number:
        logger.warning("graph_store_out_of_order", extra={"event": "graph_store_out_of_order"})
        return False
    rows = GraphElement.objects.filter(application_id=app_id)
    if latest is None:
        # Nothing recorded: any rows left are from versions since deleted.
        rows.delete()
    derived = derive(revision.data)
    close, create = [], []
    for element in ELEMENTS:
        items = derived[element]
        opened = list(
            rows.filter(element=element, last_number__isnull=True)
            .order_by("key")
            .values_list("pk", "digest", "key")
        )
        plan = placed(items, opened)
        if plan is None:
            # No room left between two rows that stay: write this list afresh.
            logger.info("graph_store_rewritten", extra={"event": "graph_store_rewritten"})
            plan = spaced(len(items)), [False] * len(items), set()
        keys, stays, kept = plan
        close.extend(pk for pk, _, _ in opened if pk not in kept)
        create.extend(
            GraphElement(
                application_id=app_id,
                element=element,
                digest=digest,
                key=key,
                body=body,
                first_number=number,
                **columns,
            )
            for (body, digest, columns), key, stay in zip(items, keys, stays, strict=True)
            if not stay
        )
    for start in range(0, len(close), BATCH):
        GraphElement.objects.filter(pk__in=close[start : start + BATCH]).update(
            last_number=number - 1
        )
    GraphElement.objects.bulk_create(create, batch_size=BATCH)
    GraphRevision.objects.filter(pk=revision.pk).update(stored=True)
    revision.stored = True
    return True


def rows_for(revision, element):
    """The rows of one element of a recorded version, in order, or None when not recorded."""
    if not revision.stored:
        return None
    return in_version(revision, element).order_by("key")


def in_version(revision, element):
    number = revision.number
    return GraphElement.objects.filter(
        Q(last_number__isnull=True) | Q(last_number__gte=number),
        application_id=revision.application_id,
        element=element,
        first_number__lte=number,
    )


def read(revision, element):
    """A recorded version's elements of one kind, exactly as its JSON lists them."""
    rows = rows_for(revision, element)
    if rows is None:
        return None
    return list(rows.values_list("body", flat=True))


def recorded_sources(revision):
    """The sources a version read, without reading the version's whole graph."""
    found = read(revision, "sources")
    return found if found is not None else revision.data.get("sources", [])


def verify(revision):
    """Whether a recorded version's rows read back exactly as its data, order included."""
    data = revision.data or {}
    for element in ELEMENTS:
        found = read(revision, element)
        expected = [body for body in data.get(element) or [] if isinstance(body, dict)]
        if found is None or found != expected:
            return False
    return True


def mismatched():
    """Recorded versions whose rows are not exactly their data: what `--check` fails on."""
    return [
        revision.pk
        for revision in GraphRevision.objects.filter(stored=True).iterator(chunk_size=20)
        if not verify(revision)
    ]


def behind():
    """Versions that can still be recorded: newer than every recorded one of their application."""
    pending = []
    for app_id in (
        GraphRevision.objects.filter(stored=False)
        .order_by()
        .values_list("application_id", flat=True)
        .distinct()
    ):
        latest = newest_recorded(app_id)
        unrecorded = GraphRevision.objects.filter(application_id=app_id, stored=False)
        if latest is not None:
            unrecorded = unrecorded.filter(number__gt=latest)
        pending.extend(unrecorded.order_by("number").values_list("pk", flat=True))
    return pending


def backfill(limit=None):
    """Record versions saved before rows existed, oldest first per application."""
    done = 0
    for pk in behind()[:limit]:
        with transaction.atomic():
            revision = GraphRevision.objects.select_for_update().get(pk=pk)
            if not revision.stored and record(revision):
                done += 1
    return done


#: Versions recorded per maintenance pass. Each reads one version's whole JSON.
BACKFILL_BATCH = 5


def record_step():
    """The maintenance lane's share: a few versions a pass. True when it did work."""
    done = backfill(BACKFILL_BATCH)
    if done:
        logger.info("graph_store_backfill", extra={"event": "graph_store_backfill"})
    return bool(done)


# ── keeping old versions small ────────────────────────────────────────────────

#: Versions of an application kept whole, newest first, besides the latest
#: published one. Older ones keep their lists only as rows.
KEEP_WHOLE = 3
#: Versions slimmed per maintenance pass. Each is read back in full first.
SLIM_BATCH = 5


def inflate(revision, data):
    """`data` with its lists read back from rows; what `GraphRevision.from_db` calls."""
    whole = {key: value for key, value in data.items() if key != "slimmed"}
    for element in ELEMENTS:
        found = read(revision, element)
        if found is None:
            # Never slimmed without rows; if they are gone, say so loudly
            # rather than answer from an empty graph.
            raise RuntimeError(f"Graph version {revision.number} has no {element} rows.")
        whole[element] = found
    return whole


def slimmable(app_id):
    """Recorded versions of one application that may give up their lists."""
    revisions = GraphRevision.objects.filter(application_id=app_id)
    whole = list(revisions.order_by("-number").values_list("pk", flat=True)[:KEEP_WHOLE])
    published = (
        revisions.filter(published_at__isnull=False)
        .order_by("-published_at")
        .values_list("pk", flat=True)
        .first()
    )
    return (
        revisions.filter(stored=True, data__slimmed__isnull=True)
        # Never a None in the list: NOT IN (..., NULL) is never true, so a
        # missing published version would have slimmed nothing at all.
        .exclude(pk__in=[pk for pk in [*whole, published] if pk is not None])
        .order_by("number")
    )


def slim(revision):
    """Drop a recorded version's lists from its JSON, once they read back exactly.

    Under the application's lock, like `record`, and only after `verify`: the
    rows are then the version, and the JSON was a second copy of it.
    """
    with transaction.atomic():
        list(
            Application.objects.select_for_update()
            .filter(pk=revision.application_id)
            .values_list("pk")
        )
        fresh = GraphRevision.objects.select_for_update().filter(pk=revision.pk).first()
        if fresh is None or not fresh.stored or fresh.data.get("slimmed") is not None:
            return False
        if not verify(fresh):
            logger.warning("graph_store_not_slimmed", extra={"event": "graph_store_not_slimmed"})
            return False
        kept = {key: value for key, value in fresh.data.items() if key not in ELEMENTS}
        GraphRevision.objects.filter(pk=fresh.pk).update(data={**kept, "slimmed": True})
    return True


def slim_step():
    """The maintenance lane's share: a few old versions a pass. True when it did work."""
    done = 0
    for app_id in (
        GraphRevision.objects.filter(stored=True, data__slimmed__isnull=True)
        .order_by()
        .values_list("application_id", flat=True)
        .distinct()
    ):
        for revision in slimmable(app_id)[: SLIM_BATCH - done]:
            done += slim(revision)
        if done >= SLIM_BATCH:
            break
    if done:
        logger.info("graph_store_slimmed", extra={"event": "graph_store_slimmed", "versions": done})
    return bool(done)


# ── answering from rows ───────────────────────────────────────────────────────


def matching(rows, column, values):
    """Rows whose padded `column` holds any of `values` as a whole word.

    PostgreSQL reads the array generated from the column (migration 0089) through
    its GIN index; SQLite matches words in the padded text.
    """
    values = list(values)
    if not values:
        return rows.none()
    if connection.vendor == "postgresql":
        table = GraphElement._meta.db_table
        return rows.extra(
            where=[f'"{table}"."{column}_array" && %s::text[]'], params=[values]
        )
    condition = Q()
    for value in values:
        condition |= Q(**{f"{column}__contains": f" {value} "})
    return rows.filter(condition)


#: The per-version figures BM25 needs from every edge, kept per process: a
#: version's rows never change once recorded.
_statistics = {}


def statistics(revision):
    """(texts, total length) over a version's edges with text, as `prepare` counts them."""
    key = (revision.pk, revision.created_at)
    found = _statistics.get(key)
    if found is None:
        found = in_version(revision, "edges").exclude(text="").aggregate(
            texts=Count("pk"), length=Sum("length")
        )
        found = (found["texts"] or 0, found["length"] or 0)
        if len(_statistics) > 64:
            _statistics.clear()
        _statistics[key] = found
    return found




class RowEdge(dict):
    """An edge's body with the row it came from, so copies of one edge stay apart."""

    row = None
    key = None


def edge_of(pk, key, body):
    edge = RowEdge(body)
    edge.row, edge.key = pk, key
    return edge


class RowsGraph:
    """`graph_ai.InMemory`'s questions, answered from a recorded version's rows.

    The same candidates in the same order: the edges holding any of the
    question's terms, ranked by `ranking.BM25.rank_covering` with the whole
    version's figures (`statistics`), ties by list order; the walk along typed
    edges and the one-hop expansion read the rows touching the nodes they reach,
    in list order; cross-references read the rows naming the question's
    references. Nothing else of the version is read. The scorecard measures
    that it answers exactly as the JSON does.
    """

    def __init__(self, revision):
        self.revision = revision
        self.order, self.place = [], {}

    def edges(self):
        return in_version(self.revision, "edges").exclude(text="")

    def ranked(self, question):
        from .ranking import BM25, query_terms

        query = query_terms(question)
        rows = list(
            matching(self.edges(), "terms", query)
            .order_by("key")
            .values_list("pk", "key", "body", "text", "terms", "length")
        )
        index = BM25(counts=[(Counter(terms.split()), length) for *_, terms, length in rows])
        texts, length = statistics(self.revision)
        index.population, index.average = texts, (length / texts if texts else 0)
        self.order = [position for _, position in index.rank_covering(query)]
        ranked = [(edge_of(*rows[p][:3]), rows[p][3]) for p in self.order]
        self.place = {edge.row: place for place, (edge, _) in enumerate(ranked)}
        return ranked

    def touching(self, nodes, typed=False):
        rows = self.edges().filter(Q(start_node__in=nodes) | Q(end_node__in=nodes))
        if typed:
            rows = rows.filter(typed=True)
        return [
            (edge_of(pk, key, body), text)
            for pk, key, body, text in rows.order_by("key").values_list("pk", "key", "body", "text")
        ]

    def walk(self, ranked):
        """`graph_ai.walk`, reading the typed edges around each hop's nodes."""
        from .graph_ai import MAX_FANOUT, MAX_HOPS, WALK_SEEDS

        starts = [(edge, text) for edge, text in ranked if edge.get("inferred")][:WALK_SEEDS]
        used = {edge.row for edge, _ in starts}
        frontier = {}
        for edge, text in starts:
            for end in (edge["source"], edge["target"]):
                frontier.setdefault(end, [text])
        reached, visited = [], set(frontier)
        for hop in range(1, MAX_HOPS + 1):
            around = {}
            if frontier:
                for edge, text in self.touching(list(frontier), typed=True):
                    # Both ends, as `graph_ai.walk` builds it - a loop is listed twice there too.
                    around.setdefault(edge["source"], []).append((edge, text))
                    around.setdefault(edge["target"], []).append((edge, text))
            following = {}
            for node_id, path in frontier.items():
                edges = around.get(node_id, [])
                if len(edges) > MAX_FANOUT:
                    continue
                for edge, text in edges:
                    if edge.row in used:
                        continue
                    used.add(edge.row)
                    step = path + [text]
                    reached.append((edge, text, hop, step))
                    other = edge["target"] if edge["source"] == node_id else edge["source"]
                    if other not in visited:
                        visited.add(other)
                        following[other] = step
            frontier = following
        return reached

    def source_ids(self):
        return [source["id"] for source in recorded_sources(self.revision)]

    def referenced(self, question, cited):
        """`graph_ai.referenced`, reading only the rows that name the question's references."""
        from .ranking import identifiers

        named = set(identifiers(question))
        if not named:
            return []
        hits = []
        rows = matching(self.edges(), "names", named).order_by("key")
        for pk, key, body, text, names in rows.values_list("pk", "key", "body", "text", "names"):
            shared = named.intersection(names.split())
            if shared:
                again = body.get("knowledge_id") in cited
                rank = self.place.get(pk, len(self.order))
                hits.append((-len(shared), again, rank, key, edge_of(pk, key, body), text, shared))
        hits.sort(key=lambda hit: hit[:4])
        return [
            (edge, text, 1, [f"names {', '.join(sorted(n.upper() for n in shared))}"])
            for *_, edge, text, shared in hits
        ]
