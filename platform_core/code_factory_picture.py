"""The before-and-after picture of a Code Factory change, drawn from the snapshot.

Reads only; split out of `code_factory_build`.
"""




#: How much one before/after picture draws. Past this it stops being a picture
#: of the change and becomes the whole graph again.
PICTURE_KNOWLEDGE = 4
PICTURE_DEPENDENCIES = 6
PICTURE_ROW = 40
#: The drawing's width, and half a node box. Small on purpose: the two drawings
#: sit side by side, so every unit here is shared by both.
PICTURE_WIDTH = 480
HALF_BOX = 60


def shown_paths(columns):
    return [key for column in columns[1:] for _, key, _ in column]


def change_picture(read, before, after, touched, was, now, connections):
    """Two drawings of the same neighbourhood - before the run and after it.

    Three columns: knowledge items on the left, the files the run changed in
    the middle, the code they depend on on the right. Both drawings share every
    position, so what moved is the only thing that differs. Coordinates are
    computed here because a CSP of `style-src 'self'` leaves SVG attributes as
    the only way to place anything.
    """
    from .code_knowledge import links

    changed = touched[:4]
    seen, chosen = set(), []
    for row in connections:
        for item in row["items"]:
            label = item["node_label"]
            if item["node_id"] not in seen and label not in {c["label"] for c in chosen}:
                seen.add(item["node_id"])
                chosen.append({"id": item["node_id"], "label": label})
    chosen = chosen[:PICTURE_KNOWLEDGE]

    outgoing = [edge for edge in (was | now) if edge[0] in changed and edge[1] not in changed]
    linked_paths = {}
    for node in chosen:
        for snapshot, key in ((before, "before"), (after, "after")):
            found, _ = links(read, snapshot, node_id=node["id"])
            linked_paths.setdefault(node["id"], {})[key] = {item["path"] for item in found}
    any_linked = set().union(*(set().union(*paths.values()) for paths in linked_paths.values()))
    ranked = sorted(
        {edge[1] for edge in outgoing},
        key=lambda path: (
            not any(edge[1] == path for edge in now - was),
            path not in any_linked,
            path,
        ),
    )
    dependencies = ranked[:PICTURE_DEPENDENCIES]

    def short(text, size=17):
        text = text.removeprefix("#: ").strip()
        return text if len(text) <= size else text[: size - 1] + "…"

    columns = (
        [("knowledge", node["id"], node["label"]) for node in chosen],
        [("changed", path, path) for path in changed],
        [("code", path, path) for path in dependencies],
    )
    height = max(len(column) for column in columns) * PICTURE_ROW + 12
    xs = (HALF_BOX + 8, PICTURE_WIDTH // 2, PICTURE_WIDTH - HALF_BOX - 8)
    existed = set(before.files.filter(path__in=shown_paths(columns)).values_list("path", flat=True))
    places, nodes = {}, []
    for column, x in zip(columns, xs, strict=True):
        top = (height - len(column) * PICTURE_ROW) / 2 + PICTURE_ROW / 2
        for index, (kind, key, label) in enumerate(column):
            y = round(top + index * PICTURE_ROW, 1)
            places[key] = (x, y)
            name = short(label) if kind == "knowledge" else short(label.rsplit("/", 1)[-1])
            nodes.append(
                {
                    "kind": kind, "x": x, "y": y, "label": name, "title": label,
                    "box_x": x - HALF_BOX, "box_y": round(y - 14, 1), "text_y": round(y + 4.5, 1),
                    # A file the run created is drawn only after it.
                    "created": kind != "knowledge" and key not in existed,
                }
            )

    def curve(start, end):
        (x1, y1), (x2, y2) = places[start], places[end]
        if x1 == x2:
            bulge = x1 + HALF_BOX + 35
            return (
                f"M{x1 + HALF_BOX},{y1} C{bulge},{y1} {bulge},{y2} {x2 + HALF_BOX},{y2}"
            )
        left, right = (
            (x1 + HALF_BOX, x2 - HALF_BOX) if x1 < x2 else (x1 - HALF_BOX, x2 + HALF_BOX)
        )
        middle = (left + right) / 2
        return f"M{left},{y1} C{middle},{y1} {middle},{y2} {right},{y2}"

    def state(in_before, in_after):
        return "same" if in_before and in_after else ("new" if in_after else "gone")

    shown = set(changed) | set(dependencies)
    edges = []
    for source, target, kind in sorted(was | now):
        if source in places and target in places and source in changed:
            edges.append(
                {
                    "d": curve(source, target),
                    "kind": "code",
                    "label": kind,
                    "state": state((source, target, kind) in was, (source, target, kind) in now),
                }
            )
    for node in chosen:
        paths = linked_paths[node["id"]]
        for path in sorted((paths["before"] | paths["after"]) & shown):
            edges.append(
                {
                    "d": curve(node["id"], path),
                    "kind": "link",
                    "label": "shared identifier",
                    "state": state(path in paths["before"], path in paths["after"]),
                }
            )
    return {
        "width": PICTURE_WIDTH,
        "height": height,
        "nodes": nodes,
        "before": [edge for edge in edges if edge["state"] != "new"],
        "after": [edge for edge in edges if edge["state"] != "gone"],
        "new_count": sum(1 for edge in edges if edge["state"] == "new"),
        "gone_count": sum(1 for edge in edges if edge["state"] == "gone"),
    }


def before_after(run):
    """What this application knew before the run, and what it knows now.

    Read from the record, never assumed: the snapshot the run reasoned about
    against the newest one of the same repository, the knowledge version it read
    against the newest built, and - per changed file - the knowledge items that
    are linked to it now and were not before. None until the change has left
    this platform; `pending` while the re-index it asked for is still running.
    """
    from .code_knowledge import links
    from .models import GraphRevision

    if not run.delivered or run.code_snapshot_id is None:
        return None
    before = run.code_snapshot
    after = before.repository.snapshots.first()
    revisions = GraphRevision.objects.filter(application_id=run.application_id)
    read = revisions.filter(number=run.graph_version).first() if run.graph_version else None
    latest = revisions.first()
    knowledge = {"before": read, "after": latest if latest and latest != read else None}
    if after is None or after.pk == before.pk:
        return {
            "pending": "code" in (run.refresh_scope or []),
            "before": before,
            "knowledge": knowledge,
        }

    old = dict(before.files.values_list("path", "digest"))
    new = dict(after.files.values_list("path", "digest"))
    touched = sorted(path for path, digest in new.items() if old.get(path) != digest)

    def edges(snapshot):
        return set(
            snapshot.relationships.values_list("source__path", "target__path", "kind")
        )

    was, now = edges(before), edges(after)
    connections = []
    if read:
        earlier = dict(before.files.filter(path__in=touched).values_list("path", "pk"))
        for file in after.files.filter(path__in=touched).only("pk", "path"):
            found, _ = links(read, after, file_id=file.pk)
            known = set()
            if file.path in earlier:
                earlier_links, _ = links(read, before, file_id=earlier[file.path])
                known = {item["node_id"] for item in earlier_links}
            gained = {}
            for item in found:
                if item["node_id"] not in known:
                    gained.setdefault(item["node_id"], item)
            if gained:
                # Identifiers first - T-03, BR-05, C-09 - because they are what a
                # reader recognises; long passages that merely mention one follow.
                # One row per label, since a table repeats the same cell.
                ranked = sorted(
                    gained.values(),
                    key=lambda item: (len(item["node_label"]) > 24, item["node_label"]),
                )
                shown = list({item["node_label"]: item for item in reversed(ranked)}.values())
                shown.reverse()
                connections.append(
                    {
                        "path": file.path,
                        "file_id": file.pk,
                        "count": len(gained),
                        "items": shown[:8],
                        "more": max(len(shown) - 8, 0),
                    }
                )
    picture = (
        change_picture(read, before, after, touched, was, now, connections) if read else None
    )
    if picture:
        newest = knowledge["after"] or read
        picture["sides"] = [
            {
                "key": "before",
                "title": "Before",
                "detail": f"code snapshot v{before.number} · knowledge graph v{read.number}",
                "edges": picture["before"],
            },
            {
                "key": "after",
                "title": "After",
                "detail": f"code snapshot v{after.number} · knowledge graph v{newest.number}",
                "edges": picture["after"],
            },
        ]
    return {
        "pending": False,
        "picture": picture,
        "before": before,
        "after": after,
        "knowledge": knowledge,
        "changed": [path for path in touched if path in old],
        "added": [path for path in touched if path not in old],
        "removed": sorted(set(old) - set(new)),
        "new_edges": sorted(now - was)[:12],
        "gone_edges": sorted(was - now)[:12],
        "connections": connections,
    }
